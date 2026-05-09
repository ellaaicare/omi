import UIKit
import Flutter
import UserNotifications
import app_links
import WatchConnectivity
import AVFoundation
import Speech
import EventKit
import PushKit

import TwilioVoice
extension FlutterError: Error {}

@main
@objc class AppDelegate: FlutterAppDelegate {
  private var methodChannel: FlutterMethodChannel?
  private var appleRemindersChannel: FlutterMethodChannel?
  private var appleHealthChannel: FlutterMethodChannel?
  private let appleRemindersService = AppleRemindersService()
  private let appleHealthService = AppleHealthService()
  private static let iso8601DateFormatter = ISO8601DateFormatter()

  private var notificationTitleOnKill: String?
  private var notificationBodyOnKill: String?

  var session: WCSession?
    var flutterWatchAPI: WatchRecorderFlutterAPI?
  private var audioChunks: [Int: (Data, Double)] = [:] // (audioData, sampleRate)
  private var nextExpectedChunkIndex: Int = 0
  private var isRecordingActive: Bool = false // Track recording state to handle app restarts

  // VoIP Push Registry
  private var voipRegistry: PKPushRegistry?
  // Twilio Voice MethodChannel
  private var twilioVoiceChannel: TwilioVoiceMethodChannel?
  private var guardianModeChannel: FlutterMethodChannel?


  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)

    let audioSession = AVAudioSession.sharedInstance()
    NotificationCenter.default.addObserver(
        self,
        selector: #selector(handleAudioSessionInterruption),
        name: AVAudioSession.interruptionNotification,
        object: audioSession
    )

    NotificationCenter.default.addObserver(
        self,
        selector: #selector(handleAudioSessionRouteChange),
        name: AVAudioSession.routeChangeNotification,
        object: audioSession
    )

    NotificationCenter.default.addObserver(
        self,
        selector: #selector(handleApplicationDidBecomeActive),
        name: UIApplication.didBecomeActiveNotification,
        object: nil
    )
    print("AppDelegate: Audio session observers registered without activating session")


      // Get Flutter view controller
      guard let controller = window?.rootViewController as? FlutterViewController else {
          print("AppDelegate: Failed to get FlutterViewController")
          return super.application(application, didFinishLaunchingWithOptions: launchOptions)
      }

      if WCSession.isSupported() {
          session = WCSession.default
          session?.delegate = self
          session?.activate()

          flutterWatchAPI = WatchRecorderFlutterAPI(binaryMessenger: controller.binaryMessenger)
          if let session = session {
              let api: WatchRecorderHostAPI = RecorderHostApiImpl(session: session, flutterWatchAPI: flutterWatchAPI)
              WatchRecorderHostAPISetup.setUp(binaryMessenger: controller.binaryMessenger, api: api)
          }
      }

      // Retrieve the link from parameters
      if let url = AppLinks.shared.getLink(launchOptions: launchOptions) {
          // We have a link, propagate it to your Flutter app or not
          AppLinks.shared.handleLink(url: url)
          return true // Returning true will stop the propagation to other packages
      }

      //Creates a method channel to handle notifications on kill
      methodChannel = FlutterMethodChannel(name: "com.friend.ios/notifyOnKill", binaryMessenger: controller.binaryMessenger)
      methodChannel?.setMethodCallHandler { [weak self] (call, result) in
          self?.handleMethodCall(call, result: result)
      }

      // Create Apple Reminders method channel
      appleRemindersChannel = FlutterMethodChannel(name: "com.omi.apple_reminders", binaryMessenger: controller.binaryMessenger)
      appleRemindersChannel?.setMethodCallHandler { [weak self] (call, result) in
          self?.handleAppleRemindersCall(call, result: result)
      }

      // Create Apple Health method channel
      appleHealthChannel = FlutterMethodChannel(name: "com.omi.apple_health", binaryMessenger: controller.binaryMessenger)
      appleHealthChannel?.setMethodCallHandler { [weak self] (call, result) in
          self?.handleAppleHealthCall(call, result: result)
      }

      // Create Speech Recognition method channel
      let speechChannel = FlutterMethodChannel(name: "com.omi.ios/speech", binaryMessenger: controller.binaryMessenger)
      let speechHandler = SpeechRecognitionHandler()
      speechChannel.setMethodCallHandler { (call, result) in
          speechHandler.handle(call, result: result)
      }

      // Create WiFi Network plugin for device AP connection
      _ = WifiNetworkPlugin(messenger: controller.binaryMessenger)

    // here, Without this code the task will not work.
    SwiftFlutterForegroundTaskPlugin.setPluginRegistrantCallback { registry in
      GeneratedPluginRegistrant.register(with: registry)
    }

    if #available(iOS 10.0, *) {
      UNUserNotificationCenter.current().delegate = self as? UNUserNotificationCenterDelegate
    }

    // MARK: - Push Notification Authorization
    requestNotificationAuthorization()

    // MARK: - VoIP Push Registration
    registerForVoIPPushes()

    // MARK: - CallKit Provider Initialization
    CallKitProvider.shared.setup()
    print("AppDelegate: CallKit provider initialized")

    // Initialize Twilio Voice SDK
    TwilioVoiceManager.shared.initialize()
    print("AppDelegate: Twilio Voice SDK initialized")

    // Initialize Twilio Voice MethodChannel
    twilioVoiceChannel = TwilioVoiceMethodChannel(messenger: controller.binaryMessenger)
    print("AppDelegate: Twilio Voice MethodChannel registered")

    // Guardian Mode MethodChannel
    guardianModeChannel = FlutterMethodChannel(
        name: "com.ellaaicare.ella/guardian_mode",
        binaryMessenger: controller.binaryMessenger
    )
    guardianModeChannel?.setMethodCallHandler { [weak self] (call, result) in
        self?.handleGuardianModeMethodCall(call, result: result)
    }
    print("AppDelegate: Guardian Mode MethodChannel registered")

    // Debug Events MethodChannel
    let debugEventsChannel = FlutterMethodChannel(
        name: "com.ellaaicare.ella/debug_events",
        binaryMessenger: controller.binaryMessenger
    )
    debugEventsChannel.setMethodCallHandler { (call, result) in
        switch call.method {
        case "getEvents":
            result(DebugEventBuffer.shared.asFlutterList())
        case "clearEvents":
            DebugEventBuffer.shared.clear()
            result(nil)
        default:
            result(FlutterMethodNotImplemented)
        }
    }
    print("AppDelegate: Debug Events MethodChannel registered")


    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  // MARK: - VoIP Push Registration

  /// Register for VoIP push notifications
  private func registerForVoIPPushes() {
    print("AppDelegate: Registering for VoIP push notifications")

    voipRegistry = PKPushRegistry(queue: DispatchQueue.main)
    voipRegistry?.delegate = self
    voipRegistry?.desiredPushTypes = [.voIP]

    print("AppDelegate: VoIP push registration initiated")
  }

  // MARK: - Audio Session Handlers

  @objc private func handleAudioSessionInterruption(notification: Notification) {
      guard let userInfo = notification.userInfo,
            let typeValue = userInfo[AVAudioSessionInterruptionTypeKey] as? UInt,
            let type = AVAudioSession.InterruptionType(rawValue: typeValue) else {
          return
      }

      switch type {
      case .began:
          print("AppDelegate: Audio session interrupted")
      case .ended:
          print("AppDelegate: Audio session interruption ended")
          GuardianModeManager.shared.repairIfActive(reason: "audio_interruption_ended")
      @unknown default:
          break
      }
  }

  @objc private func handleAudioSessionRouteChange(notification: Notification) {
      guard let userInfo = notification.userInfo,
            let reasonValue = userInfo[AVAudioSessionRouteChangeReasonKey] as? UInt,
            let reason = AVAudioSession.RouteChangeReason(rawValue: reasonValue) else {
          return
      }

      print("AppDelegate: Audio route changed - reason: \(reason.rawValue)")

      GuardianModeManager.shared.handleAudioRouteChange(reason: reason.rawValue)
  }

  @objc private func handleApplicationDidBecomeActive(notification: Notification) {
      GuardianModeManager.shared.repairIfActive(reason: "app_did_become_active")
  }

  // MARK: - Notification Authorization

  /// Request push notification permissions
  private func requestNotificationAuthorization() {
    print("AppDelegate: Requesting notification authorization")

    let center = UNUserNotificationCenter.current()
    center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
      if let error = error {
        print("AppDelegate: Notification authorization error: \(error.localizedDescription)")
        return
      }

      if granted {
        print("AppDelegate: Notification authorization granted")
        DispatchQueue.main.async {
          UIApplication.shared.registerForRemoteNotifications()
        }
      } else {
        print("AppDelegate: Notification authorization denied")
      }
    }
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

  private func handleAppleHealthCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    appleHealthService.handleMethodCall(call, result: result)
  }

  // MARK: - Silent Push for Apple Reminders Auto-Sync

  private let syncEventStore = EKEventStore()

  override func application(
      _ application: UIApplication,
      didReceiveRemoteNotification userInfo: [AnyHashable: Any],
      fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
  ) {
      print("AppDelegate: Received remote notification: \(userInfo)")

      // Check if it's Apple Reminders sync
      if let type = userInfo["type"] as? String, type == "apple_reminders_sync" {
          handleAppleRemindersSync(userInfo: userInfo, completionHandler: completionHandler)
          return
      }

      // Also check nested under "data" key (some FCM configurations)
      if let data = userInfo["data"] as? [String: Any],
         let type = data["type"] as? String,
         type == "apple_reminders_sync" {
          handleAppleRemindersSync(userInfo: data, completionHandler: completionHandler)
          return
      }

      super.application(application, didReceiveRemoteNotification: userInfo, fetchCompletionHandler: completionHandler)
  }

  private func handleAppleRemindersSync(
      userInfo: [AnyHashable: Any],
      completionHandler: @escaping (UIBackgroundFetchResult) -> Void
  ) {
      guard let actionItemId = userInfo["action_item_id"] as? String,
            let description = userInfo["description"] as? String else {
          completionHandler(.failed)
          return
      }

      // Check permission - handle iOS 17+ new authorization states
      let status = EKEventStore.authorizationStatus(for: .reminder)

      // iOS 17+ uses .fullAccess and .writeOnly, older iOS uses .authorized
      var hasAccess = false
      if #available(iOS 17.0, *) {
          hasAccess = status == .fullAccess || status == .writeOnly
      } else {
          hasAccess = status == .authorized
      }

      guard hasAccess else {
          completionHandler(.failed)
          return
      }

      // Parse due date
      let dueDate: Date? = {
          if let dueDateStr = userInfo["due_at"] as? String, !dueDateStr.isEmpty {
              return AppDelegate.iso8601DateFormatter.date(from: dueDateStr)
          }
          return nil
      }()

      // Create reminder
      let reminder = EKReminder(eventStore: syncEventStore)
      reminder.title = description
      reminder.notes = "From Omi"
      reminder.calendar = syncEventStore.defaultCalendarForNewReminders()

      if let due = dueDate {
          reminder.dueDateComponents = Calendar.current.dateComponents(
              [.year, .month, .day, .hour, .minute], from: due
          )
      }

      do {
          try syncEventStore.save(reminder, commit: true)

          // Notify Flutter to mark as exported via API
          DispatchQueue.main.async {
              self.appleRemindersChannel?.invokeMethod("markExported", arguments: ["action_item_id": actionItemId])
          }

          completionHandler(.newData)
      } catch {
          completionHandler(.failed)
      }
  }

  override func applicationWillTerminate(_ application: UIApplication) {
    // Remove audio session observers
    NotificationCenter.default.removeObserver(self, name: AVAudioSession.interruptionNotification, object: nil)
    NotificationCenter.default.removeObserver(self, name: AVAudioSession.routeChangeNotification, object: nil)
    NotificationCenter.default.removeObserver(self, name: UIApplication.didBecomeActiveNotification, object: nil)

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

// MARK: - PKPushRegistryDelegate

extension AppDelegate: PKPushRegistryDelegate {

    /// Called when VoIP push credentials are updated
    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        print("AppDelegate: VoIP push credentials updated for type: \(type.rawValue)")

        // Convert token to hex string for logging and backend registration
        let tokenParts = pushCredentials.token.map { String(format: "%02.2hhx", $0) }
        let token = tokenParts.joined()

        print("AppDelegate: VoIP push token: \(token)")

        // TESTING: Register VoIP token with Twilio SDK
        // TODO: Replace hardcoded token with backend fetch in production
        let testAccessToken = "***REMOVED_TWILIO_TOKEN***"

        print("AppDelegate: Clearing old VoIP registrations...")
        TwilioVoiceSDK.unregister(accessToken: testAccessToken, deviceToken: pushCredentials.token) { error in
            print("AppDelegate: Old registration cleared")

            print("AppDelegate: Registering VoIP token with Twilio SDK...")
            TwilioVoiceSDK.register(accessToken: testAccessToken, deviceToken: pushCredentials.token) { error in
                if let error = error {
                    print("AppDelegate: ❌ Twilio VoIP registration FAILED: \(error.localizedDescription)")
                } else {
                    print("AppDelegate: ✅ Twilio VoIP registration successful!")
                }
            }
        }
    }

    /// Called when VoIP push token is invalidated
    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        print("AppDelegate: VoIP push token invalidated for type: \(type.rawValue)")

        // TODO: Notify backend that token is invalid (Task 2)
    }

    /// Called when VoIP push registration fails
    func pushRegistry(_ registry: PKPushRegistry, didFailToReceiveRegistrationWithError error: Error, for type: PKPushType) {
        print("AppDelegate: VoIP push registration FAILED for type: \(type.rawValue)")
        print("AppDelegate: Registration error: \(error.localizedDescription)")
    }

    /// Called when an incoming VoIP push notification is received
    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload, for type: PKPushType, completion: @escaping () -> Void) {
        print("AppDelegate: Received VoIP push notification")

        // Request background execution time
        let backgroundTask = UIApplication.shared.beginBackgroundTask {
            print("AppDelegate: Background task expiring")
        }

        // Handle the push with VoIPPushHandler
        VoIPPushHandler.shared.handleIncomingPush(payload: payload) { success in
            if success {
                print("AppDelegate: VoIP push handled successfully")
            } else {
                print("AppDelegate: VoIP push handling failed")
            }

            // End background task
            UIApplication.shared.endBackgroundTask(backgroundTask)
        }

        completion()
    }
}

// MARK: - Plugin Registration

func registerPlugins(registry: FlutterPluginRegistry) {
  GeneratedPluginRegistrant.register(with: registry)
}

// MARK: - WCSessionDelegate

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

    // MARK: - Guardian Mode MethodChannel Handler

    private func handleGuardianModeMethodCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "start":
            do {
                try GuardianModeManager.shared.start()
                result(["status": "active"])
            } catch {
                result(FlutterError(
                    code: "START_FAILED",
                    message: "Failed to start Guardian Mode",
                    details: error.localizedDescription
                ))
            }

        case "stop":
            GuardianModeManager.shared.stop()
            result(["status": "idle"])

        case "getState":
            let state = GuardianModeManager.shared.getState()
            result([
                "status": state,
                "poller": GuardianModePollingService.shared.statusSnapshot()
            ])

        case "requestWakeAckPoll":
            let args = call.arguments as? [String: Any]
            GuardianModeManager.shared.requestWakeAckPoll(
                reason: args?["reason"] as? String ?? "wake_candidate",
                transcript: args?["transcript"] as? String,
                clientRequestedAtMs: args?["client_requested_at_ms"] as? Int
            )
            result(["status": "queued"])

        case "recordLatencyBreadcrumb":
            guard let args = call.arguments as? [String: Any],
                  let eventName = args["event_name"] as? String else {
                result(FlutterError(
                    code: "INVALID_ARGS",
                    message: "Missing event_name parameter",
                    details: nil
                ))
                return
            }
            GuardianModeManager.shared.recordWakeLatencyBreadcrumb(
                eventName: eventName,
                metadata: args["metadata"] as? [String: Any] ?? [:]
            )
            result(["status": "recorded"])

        case "injectRemoteAudioClip":
            guard let args = call.arguments as? [String: Any],
                  let audioURLString = args["audioURL"] as? String,
                  !audioURLString.isEmpty,
                  let audioURL = URL(string: audioURLString) else {
                result(FlutterError(
                    code: "INVALID_ARGS",
                    message: "Missing or invalid audioURL parameter",
                    details: nil
                ))
                return
            }

            let eventId = args["eventId"] as? String ?? "manual-\(Int(Date().timeIntervalSince1970))"

            print("AppDelegate: Injecting remote audio: \(audioURL.absoluteString)")
            GuardianModeManager.shared.injectRemoteAudio(audioURL: audioURL, eventId: eventId)
            result(["status": "injected", "url": audioURLString])

        default:
            result(FlutterMethodNotImplemented)
        }
    }
}

class SpeechRecognitionHandler: NSObject {
    private var recognitionTask: SFSpeechRecognitionTask?

    func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        if call.method == "transcribe" {
            guard let args = call.arguments as? [String: Any],
                  let path = args["filePath"] as? String else {
                result(FlutterError(code: "INVALID_ARGS", message: "Missing arguments", details: nil))
                return
            }

            let language = args["language"] as? String ?? "en-US"
            transcribe(filePath: path, language: language, result: result)
        } else {
            result(FlutterMethodNotImplemented)
        }
    }

    private func transcribe(filePath: String, language: String, result: @escaping FlutterResult) {
        // Request authorization first
        SFSpeechRecognizer.requestAuthorization { authStatus in
            if authStatus != .authorized {
                result(FlutterError(code: "UNAUTHORIZED", message: "Speech recognition not authorized", details: nil))
                return
            }

            let fileUrl = URL(fileURLWithPath: filePath)
            let localeIdentifier = language.isEmpty ? "en-US" : language
            let locale = Locale(identifier: localeIdentifier)

            guard let recognizer = SFSpeechRecognizer(locale: locale) else {
                result(FlutterError(code: "UNAVAILABLE", message: "Speech recognizer not available for locale \(localeIdentifier)", details: nil))
                return
            }

            if !recognizer.isAvailable {
                result(FlutterError(code: "UNAVAILABLE", message: "Speech recognizer service is currently unavailable", details: nil))
                return
            }

            let request = SFSpeechURLRecognitionRequest(url: fileUrl)
            request.shouldReportPartialResults = false
            request.requiresOnDeviceRecognition = true // Force on-device

            self.recognitionTask = recognizer.recognitionTask(with: request) { [weak self] (recognitionResult, error) in
                guard let self = self else { return }

                if let error = error {
                    // Check if it's just "No speech identified" which might happen with silence
                    let nsError = error as NSError
                    if nsError.domain == "kAFAssistantErrorDomain" && nsError.code == 1110 {
                         result("") // Treat as empty
                    } else {
                         result(FlutterError(code: "RECOGNITION_ERROR", message: error.localizedDescription, details: nil))
                    }
                    self.recognitionTask = nil
                    return
                }

                if let recognitionResult = recognitionResult, recognitionResult.isFinal {
                    let text = recognitionResult.bestTranscription.formattedString
                    result(text)
                    self.recognitionTask = nil
                }
            }
        }
    }

}
