//
//  WakeWordPlugin.swift
//  Runner
//
//  Ella Wake Word Detection - Native iOS Implementation
//
//  This is a SKELETON - port implementation from standalone Ella app.
//
//  Responsibilities:
//  - On-device ASR using SFSpeechRecognizer
//  - Continuous listening with Bluetooth audio support
//  - Wake word detection and Flutter notification
//  - Chime playback on detection
//

import Flutter
import Speech
import AVFoundation

/// Wake Word Plugin for Flutter integration
@objc class WakeWordPlugin: NSObject, FlutterPlugin {

    // MARK: - Properties

    private var channel: FlutterMethodChannel?
    private var speechRecognizer: SFSpeechRecognizer?
    private var audioEngine: AVAudioEngine?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    private var isListening = false
    private var lastTranscript = ""

    // Chime player
    private var chimePlayer: AVAudioPlayer?

    // MARK: - Plugin Registration

    static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "com.ella.wake_word",
            binaryMessenger: registrar.messenger()
        )
        let instance = WakeWordPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)

        print("[WakeWordPlugin] Registered")
    }

    // MARK: - Flutter Method Handler

    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "startListening":
            startListening(result: result)

        case "stopListening":
            stopListening(result: result)

        case "playChime":
            playChime(result: result)

        case "isListening":
            result(isListening)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    // MARK: - ASR Methods

    private func startListening(result: @escaping FlutterResult) {
        // Check authorization
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            guard let self = self else { return }

            DispatchQueue.main.async {
                switch status {
                case .authorized:
                    self.beginRecognition()
                    result(true)

                case .denied, .restricted, .notDetermined:
                    print("[WakeWordPlugin] Speech recognition not authorized: \(status)")
                    result(FlutterError(code: "NOT_AUTHORIZED",
                                       message: "Speech recognition not authorized",
                                       details: nil))
                @unknown default:
                    result(FlutterError(code: "UNKNOWN",
                                       message: "Unknown authorization status",
                                       details: nil))
                }
            }
        }
    }

    private func beginRecognition() {
        // TODO: Port from standalone Ella app
        //
        // Implementation steps:
        // 1. Configure AVAudioSession for Bluetooth
        // 2. Setup SFSpeechRecognizer
        // 3. Install audio tap on AVAudioEngine
        // 4. Start recognition task
        // 5. Forward transcripts to Flutter
        //
        // Key considerations:
        // - Handle audio route changes
        // - Restart recognition periodically (iOS 1-minute limit)
        // - Handle interruptions gracefully

        print("[WakeWordPlugin] TODO: Implement recognition from standalone Ella app")
        isListening = true
    }

    private func stopListening(result: @escaping FlutterResult) {
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil

        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)

        isListening = false
        result(true)

        print("[WakeWordPlugin] Stopped listening")
    }

    // MARK: - Chime Playback

    private func playChime(result: @escaping FlutterResult) {
        // TODO: Load chime from assets and play
        //
        // Implementation:
        // 1. Load chime.mp3 from Flutter assets
        // 2. Play through current audio route (speaker or Bluetooth)
        // 3. Handle completion

        print("[WakeWordPlugin] TODO: Implement chime playback")
        result(true)
    }

    // MARK: - Flutter Communication

    /// Send transcript to Flutter for wake word checking
    private func sendTranscript(_ text: String) {
        channel?.invokeMethod("onTranscript", arguments: ["text": text])
    }

    /// Notify Flutter that wake word was detected (if doing detection natively)
    private func notifyWakeWordDetected(_ word: String) {
        channel?.invokeMethod("onWakeWordDetected", arguments: ["word": word])
    }
}

// MARK: - TODO: Port from Standalone Ella App
//
// The following should be ported:
//
// 1. AVAudioSession configuration
//    ```swift
//    let session = AVAudioSession.sharedInstance()
//    try session.setCategory(.playAndRecord, mode: .voiceChat,
//                            options: [.allowBluetooth, .allowBluetoothA2DP])
//    try session.setActive(true)
//    ```
//
// 2. Continuous recognition with restart
//    - iOS limits recognition to ~1 minute
//    - Need to restart task periodically
//    - Handle "final" results vs partial
//
// 3. Audio route handling
//    - Detect Bluetooth headset connection
//    - Switch audio automatically
//    - Handle route changes during recognition
//
// 4. Background operation
//    - Request background audio capability
//    - Handle app state changes
//
// See: [standalone Ella app]/WakeWordDetector.swift
