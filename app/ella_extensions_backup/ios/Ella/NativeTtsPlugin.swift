//
//  NativeTtsPlugin.swift
//  Runner
//
//  Ella Native TTS - iOS AVSpeechSynthesizer Integration
//
//  Provides fallback TTS when backend API is unavailable.
//

import Flutter
import AVFoundation

/// Native TTS Plugin using iOS AVSpeechSynthesizer
@objc class NativeTtsPlugin: NSObject, FlutterPlugin, AVSpeechSynthesizerDelegate {

    // MARK: - Properties

    private var channel: FlutterMethodChannel?
    private let synthesizer = AVSpeechSynthesizer()
    private var isSpeaking = false

    // Callbacks for completion
    private var speakCompletion: FlutterResult?

    // MARK: - Plugin Registration

    static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "com.ella.native_tts",
            binaryMessenger: registrar.messenger()
        )
        let instance = NativeTtsPlugin()
        instance.channel = channel
        instance.synthesizer.delegate = instance
        registrar.addMethodCallDelegate(instance, channel: channel)

        print("[NativeTtsPlugin] Registered")
    }

    // MARK: - Flutter Method Handler

    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "speak":
            guard let args = call.arguments as? [String: Any],
                  let text = args["text"] as? String else {
                result(FlutterError(code: "INVALID_ARGS",
                                   message: "Missing 'text' argument",
                                   details: nil))
                return
            }
            let voice = args["voice"] as? String
            speak(text: text, voice: voice, result: result)

        case "stop":
            stop(result: result)

        case "isSpeaking":
            result(isSpeaking)

        case "getVoices":
            getVoices(result: result)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    // MARK: - TTS Methods

    private func speak(text: String, voice: String?, result: @escaping FlutterResult) {
        // Stop any current speech
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }

        // Create utterance
        let utterance = AVSpeechUtterance(string: text)

        // Set voice if specified
        if let voiceName = voice {
            utterance.voice = AVSpeechSynthesisVoice(identifier: voiceName)
                ?? AVSpeechSynthesisVoice(language: "en-US")
        } else {
            // Default to Samantha (high quality US English)
            utterance.voice = AVSpeechSynthesisVoice(identifier: "com.apple.ttsbundle.Samantha-compact")
                ?? AVSpeechSynthesisVoice(language: "en-US")
        }

        // Configure speech parameters
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        utterance.pitchMultiplier = 1.0
        utterance.volume = 1.0

        // Store completion handler
        speakCompletion = result
        isSpeaking = true

        // Speak
        synthesizer.speak(utterance)

        print("[NativeTtsPlugin] Speaking: \(text.prefix(50))...")
    }

    private func stop(result: @escaping FlutterResult) {
        synthesizer.stopSpeaking(at: .immediate)
        isSpeaking = false
        result(true)

        print("[NativeTtsPlugin] Stopped")
    }

    private func getVoices(result: @escaping FlutterResult) {
        let voices = AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language.starts(with: "en") }
            .map { voice -> [String: String] in
                return [
                    "id": voice.identifier,
                    "name": voice.name,
                    "language": voice.language
                ]
            }

        result(voices)
    }

    // MARK: - AVSpeechSynthesizerDelegate

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                          didFinish utterance: AVSpeechUtterance) {
        isSpeaking = false
        speakCompletion?(true)
        speakCompletion = nil

        print("[NativeTtsPlugin] Finished speaking")
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                          didCancel utterance: AVSpeechUtterance) {
        isSpeaking = false
        speakCompletion?(false)
        speakCompletion = nil

        print("[NativeTtsPlugin] Cancelled")
    }
}
