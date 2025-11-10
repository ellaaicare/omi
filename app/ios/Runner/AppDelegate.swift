import UIKit
import Flutter
import UserNotifications
import app_links
import WatchConnectivity
import AVFoundation


extension FlutterError: Error {}


@main
@objc class AppDelegate: FlutterAppDelegate {
  private var methodChannel: FlutterMethodChannel?
  private var appleRemindersChannel: FlutterMethodChannel?
  private let appleRemindersService = AppleRemindersService()

  private var notificationTitleOnKill: String?
  private var notificationBodyOnKill: String?

  var session: WCSession?
    var flutterWatchAPI: WatchRecorderFlutterAPI?
  private var audioChunks: [Int: (Data, Double)] = [:] // (audioData, sampleRate)
  private var nextExpectedChunkIndex: Int = 0
  private var isRecordingActive: Bool = false // Track recording state to handle app restarts

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)

      // Register Native TTS Plugin for iOS 26 compatibility
      NativeTtsPlugin.register(with: registrar(forPlugin: "NativeTtsPlugin")!)

      // Register Background Audio Player for push notifications
      BackgroundAudioPlayerPlugin.register(with: registrar(forPlugin: "BackgroundAudioPlayerPlugin")!)

      // Register On-Device ASR Plugin (Apple Speech framework)
      OnDeviceASRPlugin.register(with: registrar(forPlugin: "OnDeviceASRPlugin")!)

      if WCSession.isSupported() {
          session = WCSession.default
          session?.delegate = self
          session?.activate();

          let controller = window?.rootViewController as? FlutterViewController
            flutterWatchAPI = WatchRecorderFlutterAPI(binaryMessenger: controller!.binaryMessenger)
            let api: WatchRecorderHostAPI = RecorderHostApiImpl(session: session!, flutterWatchAPI: flutterWatchAPI)

            WatchRecorderHostAPISetup.setUp(binaryMessenger: controller!.binaryMessenger, api: api)
      }

      // Retrieve the link from parameters
    if let url = AppLinks.shared.getLink(launchOptions: launchOptions) {
      // We have a link, propagate it to your Flutter app or not
      AppLinks.shared.handleLink(url: url)
      return true // Returning true will stop the propagation to other packages
    }
    //Creates a method channel to handle notifications on kill
    let controller = window?.rootViewController as? FlutterViewController
    methodChannel = FlutterMethodChannel(name: "com.friend.ios/notifyOnKill", binaryMessenger: controller!.binaryMessenger)
    methodChannel?.setMethodCallHandler { [weak self] (call, result) in
      self?.handleMethodCall(call, result: result)
    }
    
    // Create Apple Reminders method channel
    appleRemindersChannel = FlutterMethodChannel(name: "com.omi.apple_reminders", binaryMessenger: controller!.binaryMessenger)
    appleRemindersChannel?.setMethodCallHandler { [weak self] (call, result) in
      self?.handleAppleRemindersCall(call, result: result)
    }

    // here, Without this code the task will not work.
    SwiftFlutterForegroundTaskPlugin.setPluginRegistrantCallback { registry in
      GeneratedPluginRegistrant.register(with: registry)
    }
    if #available(iOS 10.0, *) {
      UNUserNotificationCenter.current().delegate = self as? UNUserNotificationCenterDelegate
    }

    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  private func handleMethodCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
      case "setNotificationOnKillService":
        handleSetNotificationOnKillService(call: call)
      default:
        result(FlutterMethodNotImplemented)
    }
  }

  private func handleSetNotificationOnKillService(call: FlutterMethodCall) {
    NSLog("handleMethodCall: setNotificationOnKillService")
    
    if let args = call.arguments as? Dictionary<String, Any> {
      notificationTitleOnKill = args["title"] as? String
      notificationBodyOnKill = args["description"] as? String
    }
    
  }
  
  private func handleAppleRemindersCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    appleRemindersService.handleMethodCall(call, result: result)
  }
    

  override func applicationWillTerminate(_ application: UIApplication) {
    // If title and body are nil, then we don't need to show notification.
    if notificationTitleOnKill == nil || notificationBodyOnKill == nil {
      return
    }

    let content = UNMutableNotificationContent()
    content.title = notificationTitleOnKill!
    content.body = notificationBodyOnKill!
    let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
    let request = UNNotificationRequest(identifier: "notification on app kill", content: content, trigger: trigger)

    NSLog("Running applicationWillTerminate")

    UNUserNotificationCenter.current().add(request) { (error) in
      if let error = error {
        NSLog("Failed to show notification on kill service => error: \(error.localizedDescription)")
      } else {
        NSLog("Show notification on kill now")
      }
    }
    }

    private func handleAudioChunk(_ message: [String: Any]) {
        guard isRecordingActive else {
            print("Ignoring audio chunk - recording not active") // probably started recording with main omi app closed
            return
        }

        guard let audioChunk = message["audioChunk"] as? Data,
              let chunkIndex = message["chunkIndex"] as? Int,
              let isLast = message["isLast"] as? Bool,
              let sampleRate = message["sampleRate"] as? Double else {
            return
        }

        audioChunks[chunkIndex] = (audioChunk, sampleRate)

        if isLast {
            reassembleAndSendAudioData()
        } else {
            // Prepend 3 dummy bytes so downstream can uniformly strip headers
            var prefixedChunk = Data([0x00, 0x00, 0x00])
            prefixedChunk.append(audioChunk)
            let flutterData = FlutterStandardTypedData(bytes: prefixedChunk)
            self.flutterWatchAPI?.onAudioChunk(audioChunk: flutterData, chunkIndex: Int64(chunkIndex), isLast: isLast, sampleRate: sampleRate) { result in
                switch result {
                case .success:
                    break
                case .failure(let error):
                    print("Audio chunk \(chunkIndex) sent to Flutter - Error: \(error.message)")
                }
            }
        }
    }

    private func reassembleAndSendAudioData() {
        // Sort chunks by index and combine them
        let sortedChunks = audioChunks.sorted(by: { $0.key < $1.key })
        var combinedData = Data()
        var sampleRate: Double = 48000.0 // Default fallback

        for (_, chunkTuple) in sortedChunks {
            let (chunkData, chunkSampleRate) = chunkTuple
            combinedData.append(chunkData)
            sampleRate = chunkSampleRate
        }

        // Prepend 3 dummy bytes for full buffer as well
        var prefixed = Data([0x00, 0x00, 0x00])
        prefixed.append(combinedData)
        let flutterData = FlutterStandardTypedData(bytes: prefixed)
        self.flutterWatchAPI?.onAudioData(audioData: flutterData) { result in
            switch result {
            case .success:
                break
            case .failure(let error):
                print("Complete audio data sent to Flutter - Error: \(error.message)")
            }
        }

        audioChunks.removeAll()
        nextExpectedChunkIndex = 0
    }
}

func registerPlugins(registry: FlutterPluginRegistry) {
  GeneratedPluginRegistrant.register(with: registry)
}

extension AppDelegate: WCSessionDelegate {
    
    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) { }
    
    func sessionDidBecomeInactive(_ session: WCSession) {
        print("Session Watch Become Inactive")
    }
    
    func sessionDidDeactivate(_ session: WCSession) {
        print("Session Watch Deactivate")
    }
    
    // Receive a message from watch (foreground/active)
    func session(_ session: WCSession, didReceiveMessage message: [String : Any]) {
        Task {
            guard let method = message["method"] as? String else {
                return
            }

            switch method {
            case "startRecording":
                self.isRecordingActive = true
                self.audioChunks.removeAll()
                self.nextExpectedChunkIndex = 0
                
                DispatchQueue.main.async {
                    self.flutterWatchAPI?.onRecordingStarted() { result in
                        switch result {
                        case .success:
                            break
                        case .failure(let error):
                            print("iOS: Recording started notification sent to Flutter - Error: \(error.message)")
                        }
                    }
                }
            case "stopRecording":
                self.isRecordingActive = false
                self.flutterWatchAPI?.onRecordingStopped() { result in
                    switch result {
                    case .success:
                        break
                    case .failure(let error):
                        print("Recording stopped on Flutter - Error: \(error.message)")
                    }
                }
            case "sendAudioData":
                if let audioData = message["audioData"] as? Data {
                    // Prepend 3 dummy bytes for single-shot audio data
                    var prefixed = Data([0x00, 0x00, 0x00])
                    prefixed.append(audioData)
                    let flutterData = FlutterStandardTypedData(bytes: prefixed)
                    self.flutterWatchAPI?.onAudioData(audioData: flutterData) { result in
                        switch result {
                        case .success:
                            break
                        case .failure(let error):
                            print("Audio data sent to Flutter - Error: \(error.message)")
                        }
                    }
                } else {
                    print("Failed to cast audioData as Data - received type: \(type(of: message["audioData"]))")
                }
            case "sendAudioChunk":
                self.handleAudioChunk(message)
            case "recordingError":
                if let error = message["error"] as? String {
                    self.flutterWatchAPI?.onRecordingError(error: error) { result in
                        switch result {
                        case .success:
                            break
                        case .failure(let error):
                            print("Recording error sent to Flutter - Error: \(error.message)")
                        }
                    }
                }
            case "microphonePermissionResult":
                if let granted = message["granted"] as? Bool {
                    self.flutterWatchAPI?.onMicrophonePermissionResult(granted: granted) { result in
                        switch result {
                        case .success:
                            break
                        case .failure(let error):
                            print("Microphone permission result sent to Flutter - Error: \(error.message)")
                        }
                    }
                }
            case "batteryUpdate":
                if let batteryLevel = message["batteryLevel"] as? Double,
                   let batteryState = message["batteryState"] as? Int {
                    UserDefaults.standard.set(batteryLevel, forKey: "watch_battery_level")
                    UserDefaults.standard.set(batteryState, forKey: "watch_battery_state")
                    UserDefaults.standard.set(Date(), forKey: "watch_battery_last_updated")
                    
                    DispatchQueue.main.async {
                        self.flutterWatchAPI?.onWatchBatteryUpdate(batteryLevel: batteryLevel, batteryState: Int64(batteryState)) { result in
                            switch result {
                            case .success:
                                break
                            case .failure(let error):
                                print("iOS: Battery update sent to Flutter - Error: \(error.message)")
                            }
                        }
                    }
                }
            case "watchInfoUpdate":
                if let name = message["name"] as? String,
                   let model = message["model"] as? String,
                   let systemVersion = message["systemVersion"] as? String,
                   let localizedModel = message["localizedModel"] as? String {

                    UserDefaults.standard.set(name, forKey: "watch_device_name")
                    UserDefaults.standard.set(model, forKey: "watch_device_model")
                    UserDefaults.standard.set(systemVersion, forKey: "watch_system_version")
                    UserDefaults.standard.set(localizedModel, forKey: "watch_localized_model")
                    UserDefaults.standard.set(Date(), forKey: "watch_info_last_updated")
                }
            default:
                print("Unknown method: \(method)")
            }
        }
    }
    
    // Receive user info from watch (background/offline)
    // Used for 1.5 second audio chunks when screen is off or app is backgrounded
    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String : Any]) {
        
        Task {
            guard let method = userInfo["method"] as? String else {
                return
            }
            
            switch method {
            case "sendAudioChunk":
                self.handleAudioChunk(userInfo)
            case "stopRecording":
                self.isRecordingActive = false
                    self.flutterWatchAPI?.onRecordingStopped() { result in
                    switch result {
                    case .success:
                        break
                    case .failure(let error):
                        print("Stop recording (background) sent to Flutter - Error: \(error.message)")
                    }
                }
            case "recordingError":
                if let error = userInfo["error"] as? String {
                    self.flutterWatchAPI?.onRecordingError(error: error) { result in
                        switch result {
                        case .success:
                            break
                        case .failure(let error):
                            print("Recording error (background) sent to Flutter - Error: \(error.message)")
                        }
                    }
                }
            case "batteryUpdate":
                if let batteryLevel = userInfo["batteryLevel"] as? Double,
                   let batteryState = userInfo["batteryState"] as? Int {
                    UserDefaults.standard.set(batteryLevel, forKey: "watch_battery_level")
                    UserDefaults.standard.set(batteryState, forKey: "watch_battery_state")
                    UserDefaults.standard.set(Date(), forKey: "watch_battery_last_updated")
                    
                    DispatchQueue.main.async {
                        self.flutterWatchAPI?.onWatchBatteryUpdate(batteryLevel: batteryLevel, batteryState: Int64(batteryState)) { result in
                            switch result {
                            case .success:
                                break
                            case .failure(let error):
                                print("iOS: Background battery update sent to Flutter - Error: \(error.message)")
                            }
                        }
                    }
                }
            case "watchInfoUpdate":
                if let name = userInfo["name"] as? String,
                   let model = userInfo["model"] as? String,
                   let systemVersion = userInfo["systemVersion"] as? String,
                   let localizedModel = userInfo["localizedModel"] as? String {
                    UserDefaults.standard.set(name, forKey: "watch_device_name")
                    UserDefaults.standard.set(model, forKey: "watch_device_model")
                    UserDefaults.standard.set(systemVersion, forKey: "watch_system_version")
                    UserDefaults.standard.set(localizedModel, forKey: "watch_localized_model")
                    UserDefaults.standard.set(Date(), forKey: "watch_info_last_updated")
                }
            default:
                print("Unknown background method: \(method)")
            }
        }
    }
}

// MARK: - Native TTS Plugin for iOS 26 Compatibility

/// Native iOS TTS plugin using AVSpeechSynthesizer
/// This provides full access to all iOS voices and works reliably on iOS 26+
public class NativeTtsPlugin: NSObject, FlutterPlugin, AVSpeechSynthesizerDelegate {
    private var synthesizer: AVSpeechSynthesizer?
    private var channel: FlutterMethodChannel?

    public static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "ella.ai/native_tts",
            binaryMessenger: registrar.messenger()
        )
        let instance = NativeTtsPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)
    }

    public func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "initialize":
            initialize(result: result)

        case "getVoices":
            getVoices(result: result)

        case "speak":
            guard let args = call.arguments as? [String: Any],
                  let text = args["text"] as? String else {
                result(FlutterError(code: "INVALID_ARGS", message: "Missing text", details: nil))
                return
            }
            let voiceId = args["voiceId"] as? String
            let rate = args["rate"] as? Float ?? 0.5
            let pitch = args["pitch"] as? Float ?? 1.0
            speak(text: text, voiceId: voiceId, rate: rate, pitch: pitch, result: result)

        case "stop":
            stop(result: result)

        case "pause":
            pause(result: result)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    private func initialize(result: @escaping FlutterResult) {
        synthesizer = AVSpeechSynthesizer()
        synthesizer?.delegate = self

        // Try to configure audio session for Bluetooth routing, but don't fail if it doesn't work
        // (The watch app may already have the audio session configured)
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .default, options: [.allowBluetooth, .allowBluetoothA2DP, .mixWithOthers])
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            NSLog("✅ Native TTS audio session configured successfully")
        } catch {
            NSLog("⚠️ Native TTS couldn't configure audio session (OSStatus \(error)), but continuing anyway")
            // Don't fail initialization - TTS will still work, just might not auto-route to Bluetooth
        }

        result(true)
    }

    private func getVoices(result: @escaping FlutterResult) {
        let voices = AVSpeechSynthesisVoice.speechVoices()

        // Convert voices to Flutter-compatible format
        let voiceList = voices.compactMap { voice -> [String: String]? in
            // Only include English voices
            guard voice.language.hasPrefix("en") else { return nil }

            return [
                "id": voice.identifier,
                "name": voice.name,
                "language": voice.language,
                "quality": qualityString(for: voice.quality)
            ]
        }

        result(voiceList)
    }

    private func speak(text: String, voiceId: String?, rate: Float, pitch: Float, result: @escaping FlutterResult) {
        guard let synthesizer = synthesizer else {
            result(FlutterError(code: "NOT_INITIALIZED", message: "TTS not initialized", details: nil))
            return
        }

        let utterance = AVSpeechUtterance(string: text)

        // Set voice
        if let voiceId = voiceId, let voice = AVSpeechSynthesisVoice(identifier: voiceId) {
            utterance.voice = voice
        } else {
            // Default to enhanced quality voice if available
            utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        }

        // Set speech parameters
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * rate
        utterance.pitchMultiplier = pitch
        utterance.volume = 1.0

        // Speak
        synthesizer.speak(utterance)
        result(true)
    }

    private func stop(result: @escaping FlutterResult) {
        synthesizer?.stopSpeaking(at: .immediate)
        result(true)
    }

    private func pause(result: @escaping FlutterResult) {
        synthesizer?.pauseSpeaking(at: .word)
        result(true)
    }

    private func qualityString(for quality: AVSpeechSynthesisVoiceQuality) -> String {
        switch quality {
        case .default:
            return "default"
        case .enhanced:
            return "enhanced"
        case .premium:
            return "premium"
        @unknown default:
            return "unknown"
        }
    }

    // MARK: - AVSpeechSynthesizerDelegate

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        channel?.invokeMethod("onStart", arguments: nil)
    }

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        channel?.invokeMethod("onComplete", arguments: nil)
    }

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        channel?.invokeMethod("onCancel", arguments: nil)
    }
}

// MARK: - Background Audio Player Plugin for Push Notifications

/// Native iOS audio player that works in background
/// Properly configures audio session for background playback
public class BackgroundAudioPlayerPlugin: NSObject, FlutterPlugin, AVAudioPlayerDelegate {
    private var audioPlayer: AVAudioPlayer?
    private var channel: FlutterMethodChannel?
    private var backgroundTaskID: UIBackgroundTaskIdentifier = .invalid

    public static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "ella.ai/background_audio",
            binaryMessenger: registrar.messenger()
        )
        let instance = BackgroundAudioPlayerPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)
    }

    public func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "playFromUrl":
            guard let args = call.arguments as? [String: Any],
                  let urlString = args["url"] as? String,
                  let url = URL(string: urlString) else {
                result(FlutterError(code: "INVALID_ARGS", message: "Missing or invalid URL", details: nil))
                return
            }
            playFromUrl(url: url, result: result)

        case "stop":
            stop(result: result)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    private func playFromUrl(url: URL, result: @escaping FlutterResult) {
        NSLog("🔊 [BackgroundAudio] Playing audio from URL: \(url)")

        // Request background execution time to ensure audio can play
        backgroundTaskID = UIApplication.shared.beginBackgroundTask {
            NSLog("⚠️ [BackgroundAudio] Background time expiring, ending task")
            UIApplication.shared.endBackgroundTask(self.backgroundTaskID)
            self.backgroundTaskID = .invalid
        }
        NSLog("🕐 [BackgroundAudio] Background task started: \(backgroundTaskID)")

        // Configure audio session for background playback
        do {
            let audioSession = AVAudioSession.sharedInstance()
            // Set category and activate for background playback
            try audioSession.setCategory(.playback, mode: .spokenAudio, options: [.mixWithOthers])
            try audioSession.setActive(true, options: [])
            NSLog("✅ [BackgroundAudio] Audio session activated successfully")
        } catch {
            NSLog("⚠️ [BackgroundAudio] Audio session error (error: \(error)), continuing anyway")
        }

        // Download and play audio
        Task {
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                NSLog("✅ [BackgroundAudio] Downloaded \(data.count) bytes")

                self.audioPlayer = try AVAudioPlayer(data: data)
                self.audioPlayer?.delegate = self
                self.audioPlayer?.prepareToPlay()

                let success = self.audioPlayer?.play() ?? false
                if success {
                    NSLog("✅ [BackgroundAudio] Audio playback started successfully")
                    result(true)
                } else {
                    NSLog("❌ [BackgroundAudio] Audio playback failed to start")
                    self.endBackgroundTask()
                    result(FlutterError(code: "PLAYBACK_ERROR", message: "Failed to start playback", details: nil))
                }
            } catch {
                NSLog("❌ [BackgroundAudio] Error downloading or playing audio: \(error)")
                self.endBackgroundTask()
                result(FlutterError(code: "DOWNLOAD_ERROR", message: "Failed to download audio", details: error.localizedDescription))
            }
        }
    }

    private func endBackgroundTask() {
        if backgroundTaskID != .invalid {
            NSLog("🕐 [BackgroundAudio] Ending background task: \(backgroundTaskID)")
            UIApplication.shared.endBackgroundTask(backgroundTaskID)
            backgroundTaskID = .invalid
        }
    }

    private func stop(result: @escaping FlutterResult) {
        audioPlayer?.stop()
        audioPlayer = nil
        result(true)
    }

    // MARK: - AVAudioPlayerDelegate

    public func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        NSLog("✅ [BackgroundAudio] Audio playback finished successfully: \(flag)")
        endBackgroundTask()
        channel?.invokeMethod("onComplete", arguments: ["success": flag])
    }

    public func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        NSLog("❌ [BackgroundAudio] Audio decode error: \(error?.localizedDescription ?? "unknown")")
        endBackgroundTask()
        channel?.invokeMethod("onError", arguments: ["error": error?.localizedDescription ?? "unknown"])
    }
}
//
//  OnDeviceASRService.swift
//  Runner
//
//  Created by Claude-iOS-Developer
//  On-device ASR using Nexa SDK + Parakeet v3 (Neural Engine accelerated)
//

import Foundation
import AVFoundation
import Speech
import CoreML

/// On-device Automatic Speech Recognition Service
/// Uses Nexa SDK with Parakeet v3 model optimized for Neural Engine
class OnDeviceASRService: NSObject {

    // MARK: - Properties

    private var audioEngine: AVAudioEngine?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let speechRecognizer: SFSpeechRecognizer?

    // Nexa SDK + Parakeet v3 (placeholder for model integration)
    // TODO: Replace with actual Nexa SDK CoreML model when available
    private var nexaParakeetModel: MLModel?

    /// Callback for when transcript segments are received
    var onTranscriptReceived: ((String, Bool) -> Void)?

    /// Callback for errors
    var onError: ((Error) -> Void)?

    /// Whether on-device ASR is available on this device
    var isAvailable: Bool {
        return SFSpeechRecognizer.authorizationStatus() == .authorized &&
               speechRecognizer != nil
    }

    /// Current recording state
    private(set) var isRecording = false

    // MARK: - Initialization

    override init() {
        // Initialize with user's preferred language (or English as default)
        // TODO: Support multi-language detection
        self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        super.init()

        NSLog("🎙️ [OnDeviceASR] Initialized with locale: en-US")
        NSLog("🎙️ [OnDeviceASR] Authorization status: \(SFSpeechRecognizer.authorizationStatus().rawValue)")

        // TODO: Load Nexa SDK Parakeet v3 CoreML model
        // loadNexaParakeetModel()
    }

    // MARK: - Model Loading

    /// Load Nexa SDK Parakeet v3 CoreML model (Neural Engine optimized)
    private func loadNexaParakeetModel() {
        // TODO: Implement Nexa SDK model loading
        // This will replace the iOS Speech framework with Nexa's Parakeet v3
        //
        // Steps:
        // 1. Download Parakeet v3 CoreML model from Nexa SDK (mlpackage format)
        // 2. Load model with MLModel
        // 3. Configure for Neural Engine (ANE) acceleration
        // 4. Set up audio preprocessing pipeline
        //
        // Example:
        // guard let modelURL = Bundle.main.url(forResource: "parakeet_v3", withExtension: "mlmodelc") else {
        //     NSLog("❌ [OnDeviceASR] Parakeet v3 model not found")
        //     return
        // }
        //
        // do {
        //     let config = MLModelConfiguration()
        //     config.computeUnits = .cpuAndNeuralEngine // Enable Neural Engine
        //     self.nexaParakeetModel = try MLModel(contentsOf: modelURL, configuration: config)
        //     NSLog("✅ [OnDeviceASR] Parakeet v3 loaded with Neural Engine acceleration")
        // } catch {
        //     NSLog("❌ [OnDeviceASR] Failed to load Parakeet v3: \(error)")
        // }

        NSLog("⚠️ [OnDeviceASR] Nexa SDK Parakeet v3 model not yet integrated - using iOS Speech framework as fallback")
    }

    // MARK: - Public Methods

    /// Request speech recognition authorization
    func requestAuthorization(completion: @escaping (Bool) -> Void) {
        SFSpeechRecognizer.requestAuthorization { authStatus in
            DispatchQueue.main.async {
                let authorized = authStatus == .authorized
                NSLog("🎙️ [OnDeviceASR] Authorization: \(authorized ? "granted" : "denied")")
                completion(authorized)
            }
        }
    }

    /// Start on-device transcription
    func startTranscription() throws {
        guard isAvailable else {
            let error = NSError(domain: "OnDeviceASR", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "Speech recognition not authorized or available"])
            NSLog("❌ [OnDeviceASR] Not available - authorization: \(SFSpeechRecognizer.authorizationStatus().rawValue)")
            throw error
        }

        // Cancel any ongoing recognition task
        if let task = recognitionTask {
            task.cancel()
            recognitionTask = nil
        }

        // Configure audio session for recording
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
        try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

        // Create recognition request
        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let recognitionRequest = recognitionRequest else {
            throw NSError(domain: "OnDeviceASR", code: 2,
                        userInfo: [NSLocalizedDescriptionKey: "Unable to create recognition request"])
        }

        // Configure request for on-device recognition
        recognitionRequest.shouldReportPartialResults = true

        // Enable on-device recognition (iOS 13+)
        if #available(iOS 13, *) {
            recognitionRequest.requiresOnDeviceRecognition = true
            NSLog("✅ [OnDeviceASR] On-device recognition enabled (iOS Speech framework)")
        }

        // Setup audio engine
        audioEngine = AVAudioEngine()
        guard let audioEngine = audioEngine else {
            throw NSError(domain: "OnDeviceASR", code: 3,
                        userInfo: [NSLocalizedDescriptionKey: "Unable to create audio engine"])
        }

        let inputNode = audioEngine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)

        // Install tap on audio input
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] (buffer, when) in
            self?.recognitionRequest?.append(buffer)
        }

        // Prepare and start audio engine
        audioEngine.prepare()
        try audioEngine.start()

        // Start recognition task
        recognitionTask = speechRecognizer?.recognitionTask(with: recognitionRequest) { [weak self] result, error in
            guard let self = self else { return }

            var isFinal = false

            if let result = result {
                let transcript = result.bestTranscription.formattedString
                isFinal = result.isFinal

                NSLog("🎙️ [OnDeviceASR] Transcript (\(isFinal ? "final" : "partial")): \(transcript)")
                self.onTranscriptReceived?(transcript, isFinal)
            }

            // Clean up when recognition ends (isFinal) or on error
            // Note: isFinal only happens ONCE at end of recognition, not per utterance

            // Handle errors
            if let error = error {
                let nsError = error as NSError

                // Ignore "No speech detected" errors (code 1110) - these are non-fatal
                // They occur during periodic restarts when there's brief silence
                if nsError.domain == "kAFAssistantErrorDomain" && nsError.code == 1110 {
                    NSLog("⚠️ [OnDeviceASR] Ignoring non-fatal error: \(error.localizedDescription)")
                    // Don't clean up - let recognition continue
                    return
                }

                // For other errors, clean up
                NSLog("❌ [OnDeviceASR] Recognition error: \(error.localizedDescription)")

                if let engine = self.audioEngine, engine.isRunning {
                    engine.stop()
                    engine.inputNode.removeTap(onBus: 0)
                }

                self.recognitionRequest = nil
                self.recognitionTask = nil
                self.onError?(error)
                return
            }

            // Clean up when recognition completes naturally (isFinal)
            if isFinal {
                // Safely clean up audio engine (may already be stopped by stopTranscription)
                if let engine = self.audioEngine, engine.isRunning {
                    engine.stop()
                    engine.inputNode.removeTap(onBus: 0)
                }

                self.recognitionRequest = nil
                self.recognitionTask = nil
            }
        }

        isRecording = true
        NSLog("✅ [OnDeviceASR] Started transcription")
    }

    /// Stop on-device transcription
    func stopTranscription() {
        NSLog("🛑 [OnDeviceASR] Stopping transcription - requesting final transcript...")

        // Safely stop audio engine and remove tap
        if let engine = audioEngine, engine.isRunning {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
        }

        // CRITICAL: Use finish() instead of cancel() to get final transcript
        recognitionRequest?.endAudio()
        recognitionTask?.finish()  // ✅ Gracefully completes and sends final transcript

        // NOTE: Don't nil out recognitionTask here!
        // The completion handler will receive the final transcript and do cleanup.
        // If we nil it out now, the callback won't fire.

        audioEngine = nil
        isRecording = false

        NSLog("🛑 [OnDeviceASR] Stopped transcription (waiting for final transcript callback)")
    }

    /// Process audio buffer with Nexa Parakeet v3 (future implementation)
    private func processWithNexaParakeet(buffer: AVAudioPCMBuffer) {
        // TODO: Implement Nexa SDK Parakeet v3 inference
        //
        // Steps:
        // 1. Convert AVAudioPCMBuffer to format expected by Parakeet v3 (16kHz PCM16)
        // 2. Preprocess audio (normalization, windowing)
        // 3. Run inference on CoreML model (Neural Engine accelerated)
        // 4. Post-process output to text
        // 5. Call onTranscriptReceived with result
        //
        // Benefits of Nexa SDK:
        // - Neural Engine acceleration (110× real-time on iPhone 16 Pro)
        // - Better battery efficiency vs generic CoreML
        // - Optimized model quantization
        // - Streaming inference support
    }

    // MARK: - Device Capability Check

    /// Check if device supports efficient on-device ASR (iPhone 12+)
    static func isDeviceCapable() -> Bool {
        // Check for Neural Engine availability (A14 chip or later)
        // iPhone 12 and newer have A14+ chips with 16-core Neural Engine

        var systemInfo = utsname()
        uname(&systemInfo)
        let machineMirror = Mirror(reflecting: systemInfo.machine)
        let identifier = machineMirror.children.reduce("") { identifier, element in
            guard let value = element.value as? Int8, value != 0 else { return identifier }
            return identifier + String(UnicodeScalar(UInt8(value)))
        }

        // iPhone models with A14+ chip (Neural Engine capable)
        let capableDevices = [
            "iPhone13,1", "iPhone13,2", "iPhone13,3", "iPhone13,4", // iPhone 12 series
            "iPhone14,2", "iPhone14,3", "iPhone14,4", "iPhone14,5", // iPhone 13 series
            "iPhone14,6", // iPhone SE 3rd gen
            "iPhone14,7", "iPhone14,8", "iPhone15,2", "iPhone15,3", // iPhone 14 series
            "iPhone15,4", "iPhone15,5", // iPhone 15 series
            "iPhone16,1", "iPhone16,2", // iPhone 15 Pro series
            "iPhone17,1", "iPhone17,2", "iPhone17,3", "iPhone17,4", // iPhone 16 series
        ]

        let isCapable = capableDevices.contains { identifier.hasPrefix($0) }
        NSLog("📱 [OnDeviceASR] Device: \(identifier), Neural Engine capable: \(isCapable)")

        return isCapable
    }

    /// Get device recommendation (on-device vs cloud)
    static func getRecommendedMode() -> String {
        if isDeviceCapable() {
            return "on_device" // Use on-device ASR with Nexa SDK
        } else {
            return "cloud" // Fallback to Deepgram cloud
        }
    }
}

// MARK: - Flutter Plugin

/// Flutter plugin for on-device ASR
public class OnDeviceASRPlugin: NSObject, FlutterPlugin {
    private var asrService: OnDeviceASRService?
    private var channel: FlutterMethodChannel?

    public static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "ella.ai/on_device_asr",
            binaryMessenger: registrar.messenger()
        )
        let instance = OnDeviceASRPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)

        NSLog("✅ [OnDeviceASR] Plugin registered")
    }

    public func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "isAvailable":
            handleIsAvailable(result: result)

        case "isDeviceCapable":
            result(OnDeviceASRService.isDeviceCapable())

        case "getRecommendedMode":
            result(OnDeviceASRService.getRecommendedMode())

        case "requestAuthorization":
            handleRequestAuthorization(result: result)

        case "startTranscription":
            handleStartTranscription(result: result)

        case "stopTranscription":
            handleStopTranscription(result: result)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    private func handleIsAvailable(result: @escaping FlutterResult) {
        if asrService == nil {
            asrService = OnDeviceASRService()
        }
        result(asrService?.isAvailable ?? false)
    }

    private func handleRequestAuthorization(result: @escaping FlutterResult) {
        if asrService == nil {
            asrService = OnDeviceASRService()
        }

        asrService?.requestAuthorization { authorized in
            result(authorized)
        }
    }

    private func handleStartTranscription(result: @escaping FlutterResult) {
        if asrService == nil {
            asrService = OnDeviceASRService()
        }

        // ALWAYS setup callbacks to ensure transcripts reach Dart
        setupCallbacks()

        do {
            try asrService?.startTranscription()
            result(true)
        } catch {
            NSLog("❌ [OnDeviceASR] Start failed: \(error.localizedDescription)")
            result(FlutterError(code: "START_FAILED",
                              message: error.localizedDescription,
                              details: nil))
        }
    }

    private func handleStopTranscription(result: @escaping FlutterResult) {
        asrService?.stopTranscription()
        result(true)
    }

    private func setupCallbacks() {
        asrService?.onTranscriptReceived = { [weak self] transcript, isFinal in
            self?.channel?.invokeMethod("onTranscript", arguments: [
                "text": transcript,
                "isFinal": isFinal
            ])
        }

        asrService?.onError = { [weak self] error in
            self?.channel?.invokeMethod("onError", arguments: [
                "error": error.localizedDescription
            ])
        }
    }
}
