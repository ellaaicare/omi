import Flutter
import Foundation

class TwilioVoiceMethodChannel: NSObject {
    private let channel: FlutterMethodChannel

    init(messenger: FlutterBinaryMessenger) {
        self.channel = FlutterMethodChannel(
            name: "twilio_voice",
            binaryMessenger: messenger
        )

        super.init()

        channel.setMethodCallHandler { [weak self] (call, result) in
            self?.handleMethodCall(call, result: result)
        }

        print("TwilioVoiceMethodChannel: Initialized")
    }

    private func handleMethodCall(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        switch call.method {
        case "startCall":
            handleStartCall(result: result)
        case "endCall":
            handleEndCall(result: result)
        case "setMuted":
            handleSetMuted(call, result: result)
        default:
            result(FlutterMethodNotImplemented)
        }
    }

    private func handleStartCall(result: @escaping FlutterResult) {
        print("TwilioVoiceMethodChannel: startCall called")
        // TODO: Implement outbound call
        result("call_started")
    }

    private func handleEndCall(result: @escaping FlutterResult) {
        print("TwilioVoiceMethodChannel: endCall called")
        // TODO: Implement end call
        result(nil)
    }

    private func handleSetMuted(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
        guard let args = call.arguments as? [String: Any],
              let muted = args["muted"] as? Bool else {
            result(FlutterError(code: "INVALID_ARGS", message: "Missing muted argument", details: nil))
            return
        }

        print("TwilioVoiceMethodChannel: setMuted called with \(muted)")
        // TODO: Implement mute toggle
        result(nil)
    }

    // Helper to send events to Flutter
    func sendEvent(_ method: String, arguments: Any?) {
        channel.invokeMethod(method, arguments: arguments)
    }
}
