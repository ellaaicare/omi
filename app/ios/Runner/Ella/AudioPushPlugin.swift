//
//  AudioPushPlugin.swift
//  Runner
//
//  Ella Audio Push - Handle audio from push notifications
//
//  This is a SKELETON - port implementation from current fork's AppDelegate.
//
//  Responsibilities:
//  - Receive audio URLs from push notifications
//  - Play audio in background
//  - Handle audio session for background playback
//

import Flutter
import AVFoundation
import UserNotifications

/// Audio Push Plugin for Flutter integration
@objc class AudioPushPlugin: NSObject, FlutterPlugin {

    // MARK: - Properties

    private var channel: FlutterMethodChannel?
    private var audioPlayer: AVAudioPlayer?
    private var isPlaying = false

    // MARK: - Plugin Registration

    static func register(with registrar: FlutterPluginRegistrar) {
        let channel = FlutterMethodChannel(
            name: "com.ella.audio_push",
            binaryMessenger: registrar.messenger()
        )
        let instance = AudioPushPlugin()
        instance.channel = channel
        registrar.addMethodCallDelegate(instance, channel: channel)

        print("[AudioPushPlugin] Registered")
    }

    // MARK: - Flutter Method Handler

    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "playAudio":
            guard let args = call.arguments as? [String: Any],
                  let urlString = args["url"] as? String else {
                result(FlutterError(code: "INVALID_ARGS",
                                   message: "Missing 'url' argument",
                                   details: nil))
                return
            }
            playAudio(urlString: urlString, result: result)

        case "stopAudio":
            stopAudio(result: result)

        case "isPlaying":
            result(isPlaying)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    // MARK: - Audio Playback

    private func playAudio(urlString: String, result: @escaping FlutterResult) {
        guard let url = URL(string: urlString) else {
            result(FlutterError(code: "INVALID_URL",
                               message: "Invalid audio URL",
                               details: nil))
            return
        }

        // Configure audio session for playback
        do {
            try configureAudioSession()
        } catch {
            print("[AudioPushPlugin] Audio session error: \(error)")
        }

        // Download and play audio
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            guard let self = self else { return }

            if let error = error {
                DispatchQueue.main.async {
                    result(FlutterError(code: "DOWNLOAD_ERROR",
                                       message: error.localizedDescription,
                                       details: nil))
                }
                return
            }

            guard let data = data else {
                DispatchQueue.main.async {
                    result(FlutterError(code: "NO_DATA",
                                       message: "No audio data received",
                                       details: nil))
                }
                return
            }

            DispatchQueue.main.async {
                do {
                    self.audioPlayer = try AVAudioPlayer(data: data)
                    self.audioPlayer?.prepareToPlay()
                    self.audioPlayer?.play()
                    self.isPlaying = true

                    print("[AudioPushPlugin] Playing audio from: \(urlString)")
                    result(true)

                } catch {
                    result(FlutterError(code: "PLAYBACK_ERROR",
                                       message: error.localizedDescription,
                                       details: nil))
                }
            }
        }
        task.resume()
    }

    private func stopAudio(result: @escaping FlutterResult) {
        audioPlayer?.stop()
        audioPlayer = nil
        isPlaying = false
        result(true)

        print("[AudioPushPlugin] Stopped audio")
    }

    // MARK: - Audio Session

    private func configureAudioSession() throws {
        let session = AVAudioSession.sharedInstance()

        try session.setCategory(
            .playback,
            mode: .default,
            options: [.allowBluetooth, .allowBluetoothA2DP]
        )

        try session.setActive(true)
    }

    // MARK: - Push Notification Handling

    /// Call this from AppDelegate when push notification received
    @objc static func handlePushNotification(userInfo: [AnyHashable: Any]) {
        guard let data = userInfo["data"] as? [String: Any],
              let type = data["type"] as? String,
              (type == "audio_message" || type == "speak_tts"),
              let audioUrl = data["audio_url"] as? String else {
            return
        }

        print("[AudioPushPlugin] Received push with audio: \(audioUrl)")

        // Notify Flutter
        // Note: This requires the FlutterEngine to be running
        // For background handling, consider using a Notification Service Extension
    }
}

// MARK: - TODO: Port from Current Fork
//
// The following should be ported from current AppDelegate.swift:
//
// 1. Background audio capability
//    - Info.plist: UIBackgroundModes = [audio]
//    - Keep audio session active in background
//
// 2. Notification Service Extension
//    - For playing audio when app is terminated
//    - Requires separate extension target
//
// 3. Audio interruption handling
//    - Pause/resume on phone calls
//    - Handle other app audio
//
// 4. Bluetooth routing
//    - Play through connected headset
//    - Handle route changes
//
// See: ios/Runner/AppDelegate.swift (audio sections)
