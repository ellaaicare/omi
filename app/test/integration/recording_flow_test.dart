import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/device_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/providers/usage_provider.dart';
import 'package:omi/utils/enums.dart';
import '../fixtures/test_data.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  group('Recording Flow Integration Tests', () {
    late CaptureProvider captureProvider;
    late DeviceProvider deviceProvider;
    late ConversationProvider conversationProvider;
    late MessageProvider messageProvider;
    late PeopleProvider peopleProvider;
    late UsageProvider usageProvider;

    setUp(() {
      captureProvider = CaptureProvider();
      deviceProvider = DeviceProvider();
      conversationProvider = ConversationProvider();
      messageProvider = MessageProvider();
      peopleProvider = PeopleProvider();
      usageProvider = UsageProvider();

      captureProvider.updateProviderInstances(
        conversationProvider,
        messageProvider,
        peopleProvider,
        usageProvider,
      );
      deviceProvider.setProviders(captureProvider);
    });

    tearDown(() {
      captureProvider.dispose();
      deviceProvider.dispose();
      conversationProvider.dispose();
      messageProvider.dispose();
      peopleProvider.dispose();
      usageProvider.dispose();
    });

    testWidgets('Start and stop recording flow', (WidgetTester tester) async {
      // Initial state: not recording
      expect(captureProvider.recordingState, RecordingState.stop);
      expect(captureProvider.hasTranscripts, false);

      // Start recording
      captureProvider.updateRecordingState(RecordingState.initialising);
      expect(captureProvider.recordingState, RecordingState.initialising);

      await tester.pumpAndSettle();

      // Recording started
      captureProvider.updateRecordingState(RecordingState.record);
      expect(captureProvider.recordingState, RecordingState.record);

      await tester.pumpAndSettle();

      // Stop recording
      captureProvider.updateRecordingState(RecordingState.stop);
      expect(captureProvider.recordingState, RecordingState.stop);

      await tester.pumpAndSettle();
    });

    testWidgets('Device recording flow', (WidgetTester tester) async {
      // Setup device
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      // Update capture provider with device
      captureProvider.updateRecordingDevice(device);
      expect(captureProvider.havingRecordingDevice, true);

      // Start device recording
      captureProvider.updateRecordingState(RecordingState.deviceRecord);
      expect(captureProvider.recordingState, RecordingState.deviceRecord);
      expect(captureProvider.recordingDeviceServiceReady, true);

      await tester.pumpAndSettle();

      // Stop recording
      captureProvider.updateRecordingState(RecordingState.stop);
      expect(captureProvider.recordingState, RecordingState.stop);

      await tester.pumpAndSettle();
    });

    testWidgets('Transcript collection flow', (WidgetTester tester) async {
      // Start recording
      captureProvider.updateRecordingState(RecordingState.record);

      // Simulate receiving transcripts
      captureProvider.setHasTranscripts(true);
      expect(captureProvider.hasTranscripts, true);

      await tester.pumpAndSettle();

      // Clear transcripts
      captureProvider.clearTranscripts();
      expect(captureProvider.hasTranscripts, false);
      expect(captureProvider.segments, isEmpty);

      await tester.pumpAndSettle();
    });

    testWidgets('Recording state transitions', (WidgetTester tester) async {
      // Stop -> Initialising
      captureProvider.updateRecordingState(RecordingState.initialising);
      expect(captureProvider.recordingState, RecordingState.initialising);

      await tester.pump();

      // Initialising -> Record
      captureProvider.updateRecordingState(RecordingState.record);
      expect(captureProvider.recordingState, RecordingState.record);

      await tester.pump();

      // Record -> Pause
      captureProvider.updateRecordingState(RecordingState.pause);
      expect(captureProvider.recordingState, RecordingState.pause);

      await tester.pump();

      // Pause -> Record
      captureProvider.updateRecordingState(RecordingState.record);
      expect(captureProvider.recordingState, RecordingState.record);

      await tester.pump();

      // Record -> Stop
      captureProvider.updateRecordingState(RecordingState.stop);
      expect(captureProvider.recordingState, RecordingState.stop);

      await tester.pumpAndSettle();
    });

    testWidgets('Connection state during recording', (WidgetTester tester) async {
      // Start recording
      captureProvider.updateRecordingState(RecordingState.record);

      // Simulate connection state changes
      captureProvider.onConnectionStateChanged(true);
      expect(captureProvider.isConnected, true);

      await tester.pump();

      // Lose connection
      captureProvider.onConnectionStateChanged(false);
      expect(captureProvider.isConnected, false);

      await tester.pump();

      // Regain connection
      captureProvider.onConnectionStateChanged(true);
      expect(captureProvider.isConnected, true);

      await tester.pumpAndSettle();
    });

    testWidgets('Full recording session with device', (WidgetTester tester) async {
      // 1. Connect device
      final device = TestDevices.createTestDevice();
      await deviceProvider.setConnectedDevice(device);
      deviceProvider.setIsConnected(true);

      expect(deviceProvider.isConnected, true);

      // 2. Setup recording device
      captureProvider.updateRecordingDevice(device);
      expect(captureProvider.havingRecordingDevice, true);

      // 3. Start recording
      captureProvider.updateRecordingState(RecordingState.deviceRecord);
      expect(captureProvider.recordingState, RecordingState.deviceRecord);

      await tester.pump();

      // 4. Simulate receiving transcripts
      captureProvider.setHasTranscripts(true);
      expect(captureProvider.hasTranscripts, true);

      await tester.pump();

      // 5. Stop recording
      captureProvider.updateRecordingState(RecordingState.stop);
      expect(captureProvider.recordingState, RecordingState.stop);

      await tester.pump();

      // 6. Disconnect device
      deviceProvider.onDeviceDisconnected();
      captureProvider.updateRecordingDevice(null);

      expect(deviceProvider.isConnected, false);
      expect(captureProvider.havingRecordingDevice, false);

      await tester.pumpAndSettle();
    });
  });
}
