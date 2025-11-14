# PRD: iOS Speaker Diarization Feature

## Status
🟢 **NEW FEATURE** - High value, medium priority

## Executive Summary
Implement real-time speaker diarization in the iOS app using on-device Swift/Core ML libraries, providing users with speaker identification during conversations without impacting transcription performance.

## Problem Statement
Currently, the Omi app transcribes conversations but doesn't identify which speaker said what. This limits the value of multi-person conversations for:
- Meeting notes (who said what)
- Interview recordings
- Group discussions
- Family conversations

## Goals
- Implement on-device speaker diarization using Swift/Core ML
- Maintain real-time performance (<100ms latency increase)
- Provide dev settings toggle to test different diarization options
- Support 2-4 speakers with 85%+ accuracy
- Zero impact on battery life for single-speaker scenarios

## Success Metrics
- ✅ 85%+ accuracy for 2-3 speakers
- ✅ <100ms additional latency
- ✅ Works offline (on-device)
- ✅ <5% battery impact for multi-speaker scenarios
- ✅ User satisfaction score >4/5 for speaker identification

## Technical Specification

### Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  AVAudioEngine (Low-Level Audio Capture)                │
└────────┬────────────────────────────────────────────────┘
         │
         ├──> SpeechAnalyzer (Apple STT)  ──> Transcripts
         │                                      │
         └──> Diarization Engine         ──────┘
              (FluidAudio/Picovoice)            │
                        │                       │
                        └───> Merge Labels <────┘
                              with Timestamps
                                    │
                                    ▼
                              Display UI:
                              "Speaker 1: Hello"
                              "Speaker 2: Hi there"
```

### 1. Diarization Library Selection

**Recommended: FluidAudio** (Primary Option)
- **Pros:**
  - Open-source (MIT license)
  - Best Swift integration
  - 50-150ms latency
  - 85-92% accuracy for 2-3 speakers
  - Active development
- **Cons:**
  - Accuracy drops with >4 speakers
  - Needs clean audio (struggles with overlapping speech)

**Alternative: Picovoice Falcon** (Fallback Option)
- **Pros:**
  - Higher accuracy (88-95%)
  - Better accent handling
  - Commercial support
- **Cons:**
  - Free tier has limits
  - Requires license for commercial use

**Alternative: Sherpa-Onnx** (Experimental Option)
- **Pros:**
  - ONNX runtime (flexible)
  - Multilingual support
- **Cons:**
  - 150-300ms latency
  - More complex integration

### 2. Implementation Plan

#### Phase 1: Core Infrastructure (Week 1)

**Add Dependencies:**
```swift
// Package.swift or Podfile
dependencies: [
    .package(url: "https://github.com/FluidInference/FluidAudio", from: "1.0.0"),
    // Fallback option
    .package(url: "https://github.com/Picovoice/falcon-ios", from: "1.0.0"),
]
```

**Create Diarization Manager:**
```swift
// lib/services/diarization/diarization_manager.swift
import AVFoundation
import FluidAudio

enum DiarizationProvider: String, CaseIterable {
    case none = "None (Off)"
    case fluidAudio = "FluidAudio (Recommended)"
    case picovoice = "Picovoice Falcon"
    case sherpaOnnx = "Sherpa-ONNX (Experimental)"
}

protocol DiarizationEngine {
    func process(_ buffer: AVAudioPCMBuffer, completion: @escaping ([SpeakerSegment]) -> Void)
    func reset()
}

struct SpeakerSegment {
    let speakerId: String  // "Speaker 1", "Speaker 2", etc.
    let startTime: TimeInterval
    let endTime: TimeInterval
    let confidence: Float
}

class DiarizationManager {
    static let shared = DiarizationManager()

    private var currentEngine: DiarizationEngine?
    private var isEnabled: Bool {
        UserDefaults.standard.diarizationProvider != .none
    }

    private var selectedProvider: DiarizationProvider {
        get {
            let rawValue = UserDefaults.standard.string(forKey: "diarizationProvider") ?? "none"
            return DiarizationProvider(rawValue: rawValue) ?? .none
        }
        set {
            UserDefaults.standard.set(newValue.rawValue, forKey: "diarizationProvider")
            initializeEngine()
        }
    }

    private init() {
        initializeEngine()
    }

    private func initializeEngine() {
        currentEngine = nil

        switch selectedProvider {
        case .none:
            break
        case .fluidAudio:
            currentEngine = FluidAudioEngine()
        case .picovoice:
            currentEngine = PicovoiceFalconEngine()
        case .sherpaOnnx:
            currentEngine = SherpaOnnxEngine()
        }
    }

    func process(_ buffer: AVAudioPCMBuffer, completion: @escaping ([SpeakerSegment]) -> Void) {
        guard isEnabled, let engine = currentEngine else {
            completion([])
            return
        }

        engine.process(buffer, completion: completion)
    }

    func setProvider(_ provider: DiarizationProvider) {
        selectedProvider = provider
    }

    func reset() {
        currentEngine?.reset()
    }
}
```

#### Phase 2: FluidAudio Integration (Week 1-2)

```swift
// lib/services/diarization/engines/fluid_audio_engine.swift
import FluidAudio

class FluidAudioEngine: DiarizationEngine {
    private let diarizer: FluidDiarizer
    private var audioBuffer: [Float] = []
    private let windowSize: Int = 16000 // 1 second at 16kHz

    init() {
        // Initialize FluidAudio diarizer
        self.diarizer = FluidDiarizer(
            sampleRate: 16000,
            minSpeakers: 2,
            maxSpeakers: 4
        )
    }

    func process(_ buffer: AVAudioPCMBuffer, completion: @escaping ([SpeakerSegment]) -> Void) {
        // Convert AVAudioPCMBuffer to Float array
        guard let channelData = buffer.floatChannelData else {
            completion([])
            return
        }

        let frameLength = Int(buffer.frameLength)
        let samples = Array(UnsafeBufferPointer(start: channelData[0], count: frameLength))

        audioBuffer.append(contentsOf: samples)

        // Process in windows
        if audioBuffer.count >= windowSize {
            let chunk = Array(audioBuffer.prefix(windowSize))
            audioBuffer.removeFirst(windowSize)

            // Run diarization
            diarizer.process(chunk) { result in
                let segments = result.segments.map { segment in
                    SpeakerSegment(
                        speakerId: "Speaker \(segment.speakerIndex + 1)",
                        startTime: segment.startTime,
                        endTime: segment.endTime,
                        confidence: segment.confidence
                    )
                }
                completion(segments)
            }
        } else {
            completion([])
        }
    }

    func reset() {
        audioBuffer.removeAll()
        diarizer.reset()
    }
}
```

#### Phase 3: Audio Pipeline Integration (Week 2)

**Update Capture Provider:**
```swift
// lib/services/audio/audio_capture_service.swift
import AVFoundation
import Speech

class AudioCaptureService {
    private let audioEngine = AVAudioEngine()
    private let speechRecognizer = SFSpeechRecognizer()
    private let diarizationManager = DiarizationManager.shared

    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    func startCapture() throws {
        let inputNode = audioEngine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)

        // Install audio tap
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, time in
            guard let self = self else { return }

            // 1. Send to Apple Speech Recognition
            self.recognitionRequest?.append(buffer)

            // 2. Send to Diarization (parallel processing)
            self.diarizationManager.process(buffer) { segments in
                // Merge with transcription results
                self.mergeTranscriptWithSpeakers(segments: segments)
            }

            // 3. Send to backend (existing WebSocket stream)
            self.streamToBackend(buffer)
        }

        audioEngine.prepare()
        try audioEngine.start()

        // Start speech recognition
        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        recognitionTask = speechRecognizer?.recognitionTask(with: recognitionRequest!) { result, error in
            if let result = result {
                // Store transcription with timestamps
                self.handleTranscriptionResult(result)
            }
        }
    }

    private func mergeTranscriptWithSpeakers(segments: [SpeakerSegment]) {
        // Match speaker segments with transcription timestamps
        for segment in segments {
            // Find transcripts in time range
            let matchingTranscripts = transcriptBuffer.filter { transcript in
                transcript.timestamp >= segment.startTime &&
                transcript.timestamp <= segment.endTime
            }

            // Assign speaker ID
            for transcript in matchingTranscripts {
                transcript.speakerId = segment.speakerId
                transcript.confidence = segment.confidence
            }
        }

        // Notify UI
        NotificationCenter.default.post(
            name: .transcriptUpdated,
            object: self,
            userInfo: ["segments": segments]
        )
    }
}
```

#### Phase 4: Dev Settings UI (Week 2)

**Add Settings Page:**
```dart
// lib/pages/settings/dev_settings_page.dart
import 'package:flutter/material.dart';
import 'package:omi/services/diarization_service.dart';

class DevSettingsPage extends StatefulWidget {
  @override
  _DevSettingsPageState createState() => _DevSettingsPageState();
}

class _DevSettingsPageState extends State<DevSettingsPage> {
  DiarizationProvider _selectedProvider = DiarizationProvider.none;

  @override
  void initState() {
    super.initState();
    _loadCurrentProvider();
  }

  Future<void> _loadCurrentProvider() async {
    final provider = await DiarizationService.getCurrentProvider();
    setState(() {
      _selectedProvider = provider;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('Developer Settings - Diarization'),
      ),
      body: ListView(
        children: [
          ListTile(
            title: Text('Speaker Diarization'),
            subtitle: Text('Identify who said what in conversations'),
          ),
          Divider(),

          // Provider Selection
          ...DiarizationProvider.values.map((provider) {
            return RadioListTile<DiarizationProvider>(
              title: Text(_getProviderTitle(provider)),
              subtitle: Text(_getProviderDescription(provider)),
              value: provider,
              groupValue: _selectedProvider,
              onChanged: (value) async {
                if (value != null) {
                  await DiarizationService.setProvider(value);
                  setState(() {
                    _selectedProvider = value;
                  });

                  // Show performance metrics
                  _showPerformanceInfo(value);
                }
              },
            );
          }).toList(),

          Divider(),

          // Performance Metrics
          if (_selectedProvider != DiarizationProvider.none)
            _buildPerformanceMetrics(),

          // Test Button
          if (_selectedProvider != DiarizationProvider.none)
            Padding(
              padding: EdgeInsets.all(16),
              child: ElevatedButton(
                onPressed: _runDiarizationTest,
                child: Text('Run Test Recording'),
              ),
            ),
        ],
      ),
    );
  }

  String _getProviderTitle(DiarizationProvider provider) {
    switch (provider) {
      case DiarizationProvider.none:
        return 'Disabled';
      case DiarizationProvider.fluidAudio:
        return 'FluidAudio (Recommended)';
      case DiarizationProvider.picovoice:
        return 'Picovoice Falcon';
      case DiarizationProvider.sherpaOnnx:
        return 'Sherpa-ONNX (Experimental)';
    }
  }

  String _getProviderDescription(DiarizationProvider provider) {
    switch (provider) {
      case DiarizationProvider.none:
        return 'No speaker identification';
      case DiarizationProvider.fluidAudio:
        return '50-150ms latency, 85-92% accuracy, free';
      case DiarizationProvider.picovoice:
        return '100-200ms latency, 88-95% accuracy, requires license';
      case DiarizationProvider.sherpaOnnx:
        return '150-300ms latency, 80-90% accuracy, multilingual';
    }
  }

  Widget _buildPerformanceMetrics() {
    return FutureBuilder<DiarizationMetrics>(
      future: DiarizationService.getMetrics(),
      builder: (context, snapshot) {
        if (!snapshot.hasData) return SizedBox.shrink();

        final metrics = snapshot.data!;
        return Card(
          margin: EdgeInsets.all(16),
          child: Padding(
            padding: EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Performance Metrics', style: TextStyle(fontWeight: FontWeight.bold)),
                SizedBox(height: 8),
                _metricRow('Average Latency', '${metrics.averageLatency.toStringAsFixed(0)}ms'),
                _metricRow('Accuracy', '${(metrics.accuracy * 100).toStringAsFixed(1)}%'),
                _metricRow('Speakers Detected', '${metrics.speakersDetected}'),
                _metricRow('Battery Impact', '${(metrics.batteryImpact * 100).toStringAsFixed(1)}%'),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _metricRow(String label, String value) {
    return Padding(
      padding: EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label),
          Text(value, style: TextStyle(fontWeight: FontWeight.bold)),
        ],
      ),
    );
  }

  Future<void> _runDiarizationTest() async {
    // Show test recording UI
    showDialog(
      context: context,
      builder: (context) => DiarizationTestDialog(provider: _selectedProvider),
    );
  }

  void _showPerformanceInfo(DiarizationProvider provider) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Diarization provider switched to ${_getProviderTitle(provider)}'),
        duration: Duration(seconds: 2),
      ),
    );
  }
}
```

#### Phase 5: UI Integration (Week 3)

**Update Transcript Display:**
```dart
// lib/widgets/transcript_widget.dart
class TranscriptSegment {
  final String text;
  final String? speakerId;
  final DateTime timestamp;
  final double confidence;

  TranscriptSegment({
    required this.text,
    this.speakerId,
    required this.timestamp,
    this.confidence = 1.0,
  });
}

class TranscriptWidget extends StatelessWidget {
  final List<TranscriptSegment> segments;

  const TranscriptWidget({required this.segments});

  @override
  Widget build(BuildContext context) {
    return ListView.builder(
      itemCount: segments.length,
      itemBuilder: (context, index) {
        final segment = segments[index];
        return ListTile(
          leading: segment.speakerId != null
              ? CircleAvatar(
                  child: Text(segment.speakerId!.substring(0, 2)),
                  backgroundColor: _getSpeakerColor(segment.speakerId!),
                )
              : null,
          title: Text(segment.text),
          subtitle: segment.speakerId != null
              ? Text(
                  '${segment.speakerId} • ${_formatTime(segment.timestamp)}',
                  style: TextStyle(fontSize: 12),
                )
              : Text(_formatTime(segment.timestamp)),
          trailing: segment.confidence < 0.8
              ? Icon(Icons.warning_amber, size: 16, color: Colors.orange)
              : null,
        );
      },
    );
  }

  Color _getSpeakerColor(String speakerId) {
    // Consistent colors for each speaker
    final colors = [
      Colors.blue,
      Colors.green,
      Colors.orange,
      Colors.purple,
    ];
    final index = int.tryParse(speakerId.replaceAll(RegExp(r'[^0-9]'), '')) ?? 0;
    return colors[index % colors.length];
  }

  String _formatTime(DateTime time) {
    return '${time.hour}:${time.minute.toString().padLeft(2, '0')}';
  }
}
```

### 3. Testing Plan

#### Unit Tests
```swift
// Tests/DiarizationTests.swift
import XCTest
@testable import Omi

class DiarizationTests: XCTestCase {
    func testFluidAudioEngine() async throws {
        let engine = FluidAudioEngine()
        let testAudio = loadTestAudio("two_speakers.wav")

        var segments: [SpeakerSegment] = []
        let expectation = expectation(description: "Diarization completes")

        engine.process(testAudio) { result in
            segments = result
            expectation.fulfill()
        }

        await fulfillment(of: [expectation], timeout: 5.0)

        // Verify 2 speakers detected
        let uniqueSpeakers = Set(segments.map { $0.speakerId })
        XCTAssertEqual(uniqueSpeakers.count, 2)

        // Verify segments have timestamps
        XCTAssertTrue(segments.allSatisfy { $0.endTime > $0.startTime })
    }

    func testLatencyRequirement() async throws {
        let engine = FluidAudioEngine()
        let testAudio = loadTestAudio("short_clip.wav")

        let startTime = Date()
        let expectation = expectation(description: "Diarization completes")

        engine.process(testAudio) { _ in
            expectation.fulfill()
        }

        await fulfillment(of: [expectation], timeout: 1.0)
        let latency = Date().timeIntervalSince(startTime) * 1000 // ms

        // Verify <100ms latency
        XCTAssertLessThan(latency, 100)
    }
}
```

#### Integration Tests
- Test with 2, 3, 4 speakers
- Test with background noise
- Test with overlapping speech
- Test battery impact (24-hour recording)
- Test memory usage

#### User Acceptance Tests
- Record 10-minute meeting with 3 speakers
- Verify speaker labels match reality
- Test on various iOS devices (iPhone 12+, iPad)
- Test with different accents

## Performance Requirements

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Latency | <100ms per chunk | XCTest performance tests |
| Accuracy (2 speakers) | >90% | Manual verification |
| Accuracy (3 speakers) | >85% | Manual verification |
| Battery Impact | <5% additional | 24-hour recording test |
| Memory Usage | <50MB additional | Instruments profiling |

## Rollout Plan

### Phase 1: Beta Testing (Week 4)
- Release to internal team via TestFlight
- Collect feedback on accuracy
- Monitor crash reports

### Phase 2: Dev Settings Release (Week 5)
- Ship to production with dev settings toggle (hidden)
- Monitor opt-in usage
- Collect performance metrics

### Phase 3: Public Beta (Week 6-8)
- Announce feature in release notes
- Encourage user feedback
- Iterate on accuracy improvements

### Phase 4: General Availability (Week 9+)
- Promote FluidAudio as default
- Make feature discoverable in settings
- Add onboarding for multi-person meetings

## Success Criteria

### MVP (Minimum Viable Product)
- [ ] FluidAudio integration working
- [ ] Dev settings toggle functional
- [ ] 2-speaker accuracy >85%
- [ ] Latency <150ms
- [ ] No crashes

### V1.0 (Full Release)
- [ ] All 3 providers implemented
- [ ] 3-speaker accuracy >85%
- [ ] Latency <100ms
- [ ] Battery impact <5%
- [ ] User satisfaction >4/5

## Future Enhancements

### V2.0
- Speaker enrollment (recognize specific people)
- Speaker naming ("Mark", "Sarah" vs "Speaker 1/2")
- Cloud fallback for complex scenarios (>4 speakers)
- Real-time speaker change notifications

### V3.0
- Speaker emotion detection
- Voice characteristics (pitch, speed)
- Integration with contacts (auto-name speakers)

## Dependencies
- iOS 17+ (for SpeechAnalyzer)
- Swift 5.9+
- FluidAudio SDK
- Picovoice Falcon SDK (optional)
- Sherpa-ONNX (optional)

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Accuracy too low | Users disable feature | Set realistic expectations, improve with V2 |
| Battery drain | User complaints | Make toggle easy to find, monitor metrics |
| Latency too high | Disrupts transcription | Make async, add buffering |
| License issues with Picovoice | Legal problems | Use FluidAudio (MIT) as primary |

## Documentation

- [ ] Dev guide: Adding diarization providers
- [ ] User guide: How to use speaker identification
- [ ] Architecture doc: Audio pipeline diagram
- [ ] Performance optimization guide

---

**Estimated Effort:** 3 weeks
**Priority:** 🟢 HIGH VALUE (New Feature)
**Risk Level:** Medium
**Dependencies:** None (independent feature)
**Assigned To:** iOS Team
**Target Completion:** 3-4 weeks
