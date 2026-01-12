//
//  VoiceV2VPlugin.swift
//  Runner
//
//  Ella Voice-to-Voice - Native iOS Implementation
//
//  This is a SKELETON - port implementation from standalone Ella app.
//
//  Responsibilities:
//  - Native mic capture (PCM16 16kHz)
//  - Send audio to Flutter for WebSocket transmission
//  - Bluetooth audio routing
//

import Flutter
import AVFoundation

/// Voice V2V Plugin for Flutter integration
@objc class VoiceV2VPlugin: NSObject, FlutterPlugin {

    // MARK: - Properties

    private var channel: FlutterMethodChannel?
    private var audioEngine: AVAudioEngine?
    private var isCapturing = false

    // Audio format: PCM16 @ 16kHz mono
    private let sampleRate: Double = 16000
    private let channels: AVAudioChannelCount = 1

    // MARK: - Plugin Registration

    static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "com.ella.voice_v2v",
            binaryMessenger: registrar.messenger()
        )
        let instance = VoiceV2VPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)

        print("[VoiceV2VPlugin] Registered")
    }

    // MARK: - Flutter Method Handler

    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "startMicCapture":
            startMicCapture(result: result)

        case "stopMicCapture":
            stopMicCapture(result: result)

        case "isCapturing":
            result(isCapturing)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    // MARK: - Mic Capture

    private func startMicCapture(result: @escaping FlutterResult) {
        guard !isCapturing else {
            result(true)
            return
        }

        do {
            // Configure audio session for Bluetooth
            try configureAudioSession()

            // Setup audio engine
            audioEngine = AVAudioEngine()
            guard let audioEngine = audioEngine else {
                result(FlutterError(code: "ENGINE_ERROR",
                                   message: "Failed to create audio engine",
                                   details: nil))
                return
            }

            let inputNode = audioEngine.inputNode
            let inputFormat = inputNode.outputFormat(forBus: 0)

            // Create converter format (PCM16 @ 16kHz mono)
            guard let outputFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: sampleRate,
                channels: channels,
                interleaved: true
            ) else {
                result(FlutterError(code: "FORMAT_ERROR",
                                   message: "Failed to create output format",
                                   details: nil))
                return
            }

            // Install tap
            // TODO: Add proper format conversion if input format differs
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, time in
                self?.processAudioBuffer(buffer, format: inputFormat)
            }

            // Start engine
            audioEngine.prepare()
            try audioEngine.start()

            isCapturing = true
            result(true)

            print("[VoiceV2VPlugin] Mic capture started")

        } catch {
            print("[VoiceV2VPlugin] Failed to start capture: \(error)")
            result(FlutterError(code: "START_ERROR",
                               message: error.localizedDescription,
                               details: nil))
        }
    }

    private func stopMicCapture(result: @escaping FlutterResult) {
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine = nil

        isCapturing = false
        result(true)

        print("[VoiceV2VPlugin] Mic capture stopped")
    }

    // MARK: - Audio Processing

    private func processAudioBuffer(_ buffer: AVAudioPCMBuffer, format: AVAudioFormat) {
        // TODO: Port from standalone Ella app
        //
        // Implementation steps:
        // 1. Convert buffer to PCM16 @ 16kHz if needed
        // 2. Extract raw bytes
        // 3. Send to Flutter via method channel
        //
        // Key considerations:
        // - Handle format conversion efficiently
        // - Chunk audio appropriately (100ms chunks typical)
        // - Handle buffer underruns

        // Placeholder: Send empty data
        // channel?.invokeMethod("onAudioData", arguments: ["data": Data()])
    }

    // MARK: - Audio Session

    private func configureAudioSession() throws {
        let session = AVAudioSession.sharedInstance()

        // Configure for voice chat with Bluetooth
        try session.setCategory(
            .playAndRecord,
            mode: .voiceChat,
            options: [.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker]
        )

        try session.setActive(true)

        print("[VoiceV2VPlugin] Audio session configured")
    }
}

// MARK: - TODO: Port from Standalone Ella App
//
// The following should be ported:
//
// 1. Proper format conversion
//    - Input may be 44.1kHz or 48kHz float
//    - Need to downsample to 16kHz
//    - Need to convert to PCM16 (Int16)
//
// 2. Efficient buffer handling
//    - Use AVAudioConverter for resampling
//    - Minimize allocations in audio callback
//    - Handle timing correctly
//
// 3. Bluetooth audio routing
//    - Detect HFP vs A2DP
//    - Handle route changes during call
//    - Support AirPods, other headsets
//
// 4. Audio interruption handling
//    - Phone calls
//    - Siri activation
//    - Other app audio
//
// See: [standalone Ella app]/GrokVoiceSession.swift
