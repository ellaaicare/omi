# PRD: App Testing Infrastructure

## Status
🔴 **CRITICAL** - Minimal test coverage currently

## Executive Summary
Build comprehensive testing infrastructure for the Flutter app to achieve 70%+ code coverage, prevent regressions, and enable confident refactoring of complex providers.

## Problem Statement
- **Current:** Only 1 integration test file
- **No unit tests** for 21 providers, services, or API layers
- **High regression risk** when refactoring
- **Manual testing** is time-consuming and error-prone

## Goals
- Achieve 70% code coverage
- Implement unit tests for all providers
- Add widget tests for key UI flows
- Add integration tests for critical user journeys
- Integrate with CI/CD

## Success Metrics
- ✅ 70% code coverage
- ✅ All providers have unit tests
- ✅ Key UI flows have widget tests
- ✅ Tests run in <3 minutes
- ✅ Integrated with CI/CD

## Technical Specification

### Test Structure
```
app/test/
├── unit/
│   ├── providers/
│   │   ├── capture_provider_test.dart
│   │   ├── device_provider_test.dart
│   │   ├── app_provider_test.dart
│   │   └── message_provider_test.dart
│   ├── services/
│   │   ├── bluetooth_service_test.dart
│   │   └── audio_service_test.dart
│   └── utils/
│       ├── encryption_test.dart
│       └── audio_processing_test.dart
├── widget/
│   ├── home_page_test.dart
│   ├── conversation_widget_test.dart
│   └── memory_card_test.dart
├── integration/
│   ├── full_capture_flow_test.dart
│   ├── device_connection_flow_test.dart
│   └── app_installation_flow_test.dart
├── mocks/
│   ├── mock_providers.dart
│   ├── mock_services.dart
│   └── mock_api_client.dart
└── fixtures/
    ├── test_audio_data.dart
    └── test_conversations.dart
```

### Implementation

**1. Add Testing Dependencies**

```yaml
# pubspec.yaml
dev_dependencies:
  flutter_test:
    sdk: flutter
  mockito: ^5.4.0
  build_runner: ^2.4.0
  mocktail: ^1.0.0
  fake_async: ^1.3.1
  integration_test:
    sdk: flutter
```

**2. Provider Unit Tests**

```dart
// test/unit/providers/capture_provider_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/mockito.dart';
import 'package:mockito/annotations.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/services/devices/device_connection.dart';

@GenerateMocks([DeviceConnection, ISocketService])
import 'capture_provider_test.mocks.dart';

void main() {
  group('CaptureProvider', () {
    late CaptureProvider provider;
    late MockDeviceConnection mockDevice;
    late MockISocketService mockSocket;

    setUp(() {
      mockDevice = MockDeviceConnection();
      mockSocket = MockISocketService();
      provider = CaptureProvider();
    });

    tearDown(() {
      provider.dispose();
    });

    test('initializes with correct default state', () {
      expect(provider.isRecording, false);
      expect(provider.segments, isEmpty);
    });

    test('starts recording and updates state', () async {
      when(mockDevice.startAudioStream()).thenAnswer((_) async => true);

      await provider.startRecording();

      expect(provider.isRecording, true);
      verify(mockDevice.startAudioStream()).called(1);
    });

    test('stops recording and cleans up resources', () async {
      when(mockDevice.stopAudioStream()).thenAnswer((_) async => true);

      provider.isRecording = true;
      await provider.stopRecording();

      expect(provider.isRecording, false);
      verify(mockDevice.stopAudioStream()).called(1);
    });

    test('handles audio chunks correctly', () async {
      final testChunk = List<int>.filled(1024, 0);

      await provider.processAudioChunk(testChunk);

      expect(provider.audioBuffer, isNotEmpty);
    });

    test('cancels all subscriptions on dispose', () async {
      // Start recording to create subscriptions
      when(mockDevice.startAudioStream()).thenAnswer((_) async => true);
      await provider.startRecording();

      // Dispose
      await provider.dispose();

      // Verify cleanup
      expect(provider.activeSubscriptions, 0);
    });

    test('notifyListeners called when state changes', () {
      var notifyCount = 0;
      provider.addListener(() => notifyCount++);

      provider.setRecordingState(true);

      expect(notifyCount, greaterThan(0));
    });
  });
}

// test/unit/providers/device_provider_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/mockito.dart';
import 'package:omi/providers/device_provider.dart';

void main() {
  group('DeviceProvider', () {
    late DeviceProvider provider;

    setUp(() {
      provider = DeviceProvider();
    });

    test('prevents concurrent connection attempts', () async {
      var connectionAttempts = 0;

      // Mock connection attempt
      provider.mockConnectionCallback = () {
        connectionAttempts++;
        return Future.delayed(Duration(milliseconds: 100));
      };

      // Attempt 5 concurrent connections
      await Future.wait([
        for (int i = 0; i < 5; i++) provider.scanAndConnectToDevice()
      ]);

      // Should only attempt once due to mutex
      expect(connectionAttempts, 1);
    });

    test('handles connection failure gracefully', () async {
      provider.mockConnectionCallback = () {
        throw Exception('Connection failed');
      };

      expect(
        () async => await provider.scanAndConnectToDevice(),
        throwsException,
      );

      // Should reset state after failure
      expect(provider.isConnecting, false);
    });
  });
}
```

**3. Widget Tests**

```dart
// test/widget/home_page_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/pages/home/page.dart';
import 'package:provider/provider.dart';

void main() {
  group('HomePage', () {
    testWidgets('displays device connection status', (WidgetTester tester) async {
      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => DeviceProvider()),
          ],
          child: MaterialApp(home: HomePage()),
        ),
      );

      expect(find.text('Not Connected'), findsOneWidget);
    });

    testWidgets('shows recording indicator when capturing', (WidgetTester tester) async {
      final captureProvider = CaptureProvider();
      captureProvider.setRecordingState(true);

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider.value(value: captureProvider),
          ],
          child: MaterialApp(home: HomePage()),
        ),
      );

      expect(find.byIcon(Icons.mic), findsOneWidget);
      expect(find.text('Recording'), findsOneWidget);
    });

    testWidgets('tapping connect button triggers connection', (WidgetTester tester) async {
      final deviceProvider = DeviceProvider();

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider.value(value: deviceProvider),
          ],
          child: MaterialApp(home: HomePage()),
        ),
      );

      await tester.tap(find.text('Connect Device'));
      await tester.pump();

      expect(deviceProvider.isConnecting, true);
    });
  });
}

// test/widget/conversation_widget_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/widgets/conversation_widget.dart';

void main() {
  group('ConversationWidget', () {
    testWidgets('displays conversation transcript', (WidgetTester tester) async {
      final testConversation = Conversation(
        id: 'test_id',
        transcript: 'Hello, this is a test.',
        createdAt: DateTime.now(),
      );

      await tester.pumpWidget(
        MaterialApp(
          home: ConversationWidget(conversation: testConversation),
        ),
      );

      expect(find.text('Hello, this is a test.'), findsOneWidget);
    });

    testWidgets('shows speaker labels if available', (WidgetTester tester) async {
      final testConversation = Conversation(
        id: 'test_id',
        segments: [
          TranscriptSegment(
            text: 'Hello',
            speakerId: 'Speaker 1',
            timestamp: DateTime.now(),
          ),
        ],
      );

      await tester.pumpWidget(
        MaterialApp(
          home: ConversationWidget(conversation: testConversation),
        ),
      );

      expect(find.text('Speaker 1'), findsOneWidget);
      expect(find.text('Hello'), findsOneWidget);
    });
  });
}
```

**4. Integration Tests**

```dart
// integration_test/full_capture_flow_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:omi/main.dart' as app;

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  group('Full Capture Flow', () {
    testWidgets('complete recording and transcription flow', (WidgetTester tester) async {
      app.main();
      await tester.pumpAndSettle();

      // 1. Navigate to home
      expect(find.text('Home'), findsOneWidget);

      // 2. Connect device
      await tester.tap(find.text('Connect Device'));
      await tester.pumpAndSettle();

      // Wait for connection
      await tester.pump(Duration(seconds: 3));

      // 3. Start recording
      await tester.tap(find.byIcon(Icons.mic));
      await tester.pumpAndSettle();

      expect(find.text('Recording'), findsOneWidget);

      // 4. Record for 5 seconds
      await tester.pump(Duration(seconds: 5));

      // 5. Stop recording
      await tester.tap(find.byIcon(Icons.stop));
      await tester.pumpAndSettle();

      // 6. Verify conversation created
      await tester.tap(find.text('Conversations'));
      await tester.pumpAndSettle();

      expect(find.byType(ConversationCard), findsAtLeastNWidgets(1));
    });
  });
}

// integration_test/device_connection_flow_test.dart
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  group('Device Connection Flow', () {
    testWidgets('scan and connect to Omi device', (WidgetTester tester) async {
      app.main();
      await tester.pumpAndSettle();

      // Navigate to device settings
      await tester.tap(find.byIcon(Icons.settings));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Devices'));
      await tester.pumpAndSettle();

      // Start scan
      await tester.tap(find.text('Scan for Devices'));
      await tester.pumpAndSettle();

      // Wait for scan
      await tester.pump(Duration(seconds: 5));

      // Verify devices found
      expect(find.byType(DeviceListTile), findsAtLeastNWidgets(1));

      // Connect to first device
      await tester.tap(find.byType(DeviceListTile).first);
      await tester.pumpAndSettle();

      // Verify connection
      expect(find.text('Connected'), findsOneWidget);
    });
  });
}
```

**5. CI/CD Integration**

```yaml
# .github/workflows/app-tests.yml
name: App Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: macos-latest

    steps:
      - uses: actions/checkout@v3

      - name: Setup Flutter
        uses: subosito/flutter-action@v2
        with:
          flutter-version: '3.35.3'

      - name: Install dependencies
        run: |
          cd app
          flutter pub get

      - name: Run unit tests
        run: |
          cd app
          flutter test --coverage

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          file: ./app/coverage/lcov.info

      - name: Check coverage threshold
        run: |
          cd app
          flutter test --coverage
          lcov --summary coverage/lcov.info | grep "lines......" | awk '{print $2}' | sed 's/%//' | awk '{if ($1 < 70) exit 1}'
```

## Implementation Plan

### Week 1: Infrastructure
- [ ] Add testing dependencies
- [ ] Create test directory structure
- [ ] Set up mocks and fixtures
- [ ] Configure CI/CD

### Week 2: Provider Unit Tests (40% coverage)
- [ ] CaptureProvider tests
- [ ] DeviceProvider tests
- [ ] AppProvider tests
- [ ] MessageProvider tests

### Week 3: Widget & Service Tests (60% coverage)
- [ ] Key widget tests
- [ ] Service layer tests
- [ ] API client tests

### Week 4: Integration Tests (70% coverage)
- [ ] Full user flow tests
- [ ] Device connection tests
- [ ] Optimize test performance

## Success Criteria

- [ ] 70% code coverage
- [ ] All providers tested
- [ ] Tests run in <3 minutes
- [ ] CI/CD integrated

---

**Estimated Effort:** 4 weeks
**Priority:** 🔴 CRITICAL
**Target Completion:** 4 weeks
