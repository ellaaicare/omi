import 'package:flutter_test/flutter_test.dart';

import 'package:omi/pages/conversation_capturing/page.dart';
import 'package:omi/utils/enums.dart';

void main() {
  test('active necklace capture keeps Process Now visible before transcript text arrives', () {
    for (final state in [
      RecordingState.initialising,
      RecordingState.deviceRecord,
      RecordingState.pause,
      RecordingState.error,
    ]) {
      expect(
        shouldShowCaptureControls(
          hasSegments: false,
          hasPhotos: false,
          recordingState: state,
        ),
        isTrue,
        reason: 'controls must remain visible while necklace state is $state',
      );
    }
  });

  test('empty active capture stops its actual transport', () {
    expect(
      emptyCaptureStopTarget(
        havingRecordingDevice: true,
        recordingState: RecordingState.deviceRecord,
      ),
      EmptyCaptureStopTarget.necklace,
    );
    expect(
      emptyCaptureStopTarget(
        havingRecordingDevice: false,
        recordingState: RecordingState.record,
      ),
      EmptyCaptureStopTarget.phone,
    );
    expect(
      emptyCaptureStopTarget(
        havingRecordingDevice: true,
        recordingState: RecordingState.stop,
      ),
      EmptyCaptureStopTarget.none,
    );
  });
}
