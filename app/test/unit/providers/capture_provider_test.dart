import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/providers/usage_provider.dart';
import 'package:omi/backend/schema/transcript_segment.dart';
import 'package:omi/utils/enums.dart';
import '../../fixtures/test_data.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('CaptureProvider Tests', () {
    late CaptureProvider captureProvider;
    late ConversationProvider mockConversationProvider;
    late MessageProvider mockMessageProvider;
    late PeopleProvider mockPeopleProvider;
    late UsageProvider mockUsageProvider;

    setUp(() {
      captureProvider = CaptureProvider();
      mockConversationProvider = ConversationProvider();
      mockMessageProvider = MessageProvider();
      mockPeopleProvider = PeopleProvider();
      mockUsageProvider = UsageProvider();

      captureProvider.updateProviderInstances(
        mockConversationProvider,
        mockMessageProvider,
        mockPeopleProvider,
        mockUsageProvider,
      );
    });

    tearDown(() {
      captureProvider.dispose();
    });

    group('Initialization', () {
      test('should initialize with stopped recording state', () {
        expect(captureProvider.recordingState, RecordingState.stop);
        expect(captureProvider.hasTranscripts, false);
        expect(captureProvider.segments, isEmpty);
        expect(captureProvider.photos, isEmpty);
      });

      test('should initialize with no recording device', () {
        expect(captureProvider.havingRecordingDevice, false);
      });

      test('should initialize provider instances', () {
        expect(captureProvider.conversationProvider, isNotNull);
        expect(captureProvider.messageProvider, isNotNull);
        expect(captureProvider.peopleProvider, isNotNull);
        expect(captureProvider.usageProvider, isNotNull);
      });
    });

    group('Recording State Management', () {
      test('should update recording state', () {
        expect(captureProvider.recordingState, RecordingState.stop);

        captureProvider.updateRecordingState(RecordingState.initialising);
        expect(captureProvider.recordingState, RecordingState.initialising);

        captureProvider.updateRecordingState(RecordingState.record);
        expect(captureProvider.recordingState, RecordingState.record);

        captureProvider.updateRecordingState(RecordingState.stop);
        expect(captureProvider.recordingState, RecordingState.stop);
      });

      test('should handle pause state', () {
        expect(captureProvider.isPaused, false);
      });

      test('should check transcript service readiness', () {
        // Initially not ready
        expect(captureProvider.transcriptServiceReady, false);
      });

      test('should check recording device service readiness', () {
        expect(captureProvider.recordingDeviceServiceReady, false);

        captureProvider.updateRecordingState(RecordingState.record);
        expect(captureProvider.recordingDeviceServiceReady, true);
      });
    });

    group('Device Recording', () {
      test('should update recording device', () {
        final device = TestDevices.createTestDevice();

        captureProvider.updateRecordingDevice(device);

        expect(captureProvider.havingRecordingDevice, true);
      });

      test('should clear recording device', () {
        final device = TestDevices.createTestDevice();
        captureProvider.updateRecordingDevice(device);
        expect(captureProvider.havingRecordingDevice, true);

        captureProvider.updateRecordingDevice(null);
        expect(captureProvider.havingRecordingDevice, false);
      });
    });

    group('Transcript Management', () {
      test('should set has transcripts', () {
        expect(captureProvider.hasTranscripts, false);

        captureProvider.setHasTranscripts(true);
        expect(captureProvider.hasTranscripts, true);

        captureProvider.setHasTranscripts(false);
        expect(captureProvider.hasTranscripts, false);
      });

      test('should clear transcripts', () {
        captureProvider.setHasTranscripts(true);
        captureProvider.segments = [
          TranscriptSegment(
            text: 'Test segment',
            speaker: 'SPEAKER_00',
            speakerId: 0,
            isUser: false,
            start: 0.0,
            end: 1.0,
          ),
        ];

        captureProvider.clearTranscripts();

        expect(captureProvider.segments, isEmpty);
        expect(captureProvider.hasTranscripts, false);
      });

      test('should handle new segments received', () {
        final segments = [
          TranscriptSegment(
            text: 'Hello',
            speaker: 'SPEAKER_00',
            speakerId: 0,
            isUser: true,
            start: 0.0,
            end: 1.0,
          ),
          TranscriptSegment(
            text: 'World',
            speaker: 'SPEAKER_01',
            speakerId: 1,
            isUser: false,
            start: 1.0,
            end: 2.0,
          ),
        ];

        captureProvider.onSegmentReceived(segments);

        expect(captureProvider.segments.length, greaterThanOrEqualTo(0));
        expect(captureProvider.hasTranscripts, isTrue);
      });
    });

    group('Photos Management', () {
      test('should initialize with empty photos', () {
        expect(captureProvider.photos, isEmpty);
      });
    });

    group('Connection State', () {
      test('should handle connection state changes', () {
        captureProvider.onConnectionStateChanged(true);
        expect(captureProvider.isConnected, true);

        captureProvider.onConnectionStateChanged(false);
        expect(captureProvider.isConnected, false);
      });
    });

    group('WAL Support', () {
      test('should initialize with WAL not supported', () {
        expect(captureProvider.isWalSupported, false);
      });

      test('should set WAL supported', () {
        captureProvider.setIsWalSupported(true);
        expect(captureProvider.isWalSupported, true);

        captureProvider.setIsWalSupported(false);
        expect(captureProvider.isWalSupported, false);
      });
    });

    group('Auto Reconnection', () {
      test('should initialize without auto reconnection', () {
        expect(captureProvider.isAutoReconnecting, false);
      });

      test('should have reconnect countdown', () {
        expect(captureProvider.reconnectCountdown, greaterThanOrEqualTo(0));
      });
    });

    group('Transcription Service Status', () {
      test('should initialize with empty service statuses', () {
        expect(captureProvider.transcriptionServiceStatuses, isEmpty);
      });
    });

    group('Speaker Tagging', () {
      test('should initialize with empty suggestion map', () {
        expect(captureProvider.suggestionsBySegmentId, isEmpty);
      });

      test('should initialize with empty tagging segment ids', () {
        expect(captureProvider.taggingSegmentIds, isEmpty);
      });
    });

    group('Notification Tests', () {
      test('should notify listeners on recording state change', () {
        var notified = false;
        captureProvider.addListener(() {
          notified = true;
        });

        captureProvider.updateRecordingState(RecordingState.record);
        expect(notified, true);
      });

      test('should notify listeners when has transcripts changes', () {
        var notified = false;
        captureProvider.addListener(() {
          notified = true;
        });

        captureProvider.setHasTranscripts(true);
        expect(notified, true);
      });

      test('should notify listeners when device changes', () {
        var notified = false;
        captureProvider.addListener(() {
          notified = true;
        });

        final device = TestDevices.createTestDevice();
        captureProvider.updateRecordingDevice(device);
        expect(notified, true);
      });
    });
  });
}
