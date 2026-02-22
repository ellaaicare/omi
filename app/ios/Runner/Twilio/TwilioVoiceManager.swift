import Foundation
import TwilioVoice

class TwilioVoiceManager: NSObject {
    static let shared = TwilioVoiceManager()

    private var activeCall: Call?
    private var audioDevice: DefaultAudioDevice?

    private override init() {
        super.init()
    }

    func initialize() {
        print("TwilioVoiceManager: Initializing Twilio Voice SDK")

        // Initialize audio device
        audioDevice = DefaultAudioDevice()
        TwilioVoiceSDK.audioDevice = audioDevice

        print("TwilioVoiceManager: Initialization complete")
    }

    func acceptCall(uuid: UUID, accessToken: String) {
        print("TwilioVoiceManager: Accepting incoming call with UUID \(uuid)")

        let connectOptions = ConnectOptions(accessToken: accessToken) { builder in
            builder.uuid = uuid
        }

        let call = TwilioVoiceSDK.connect(options: connectOptions, delegate: self)
        activeCall = call

        print("TwilioVoiceManager: Call connection initiated")
    }

    func endCall(uuid: UUID) {
        print("TwilioVoiceManager: Ending call with UUID \(uuid)")

        guard let call = activeCall, call.uuid == uuid else {
            print("TwilioVoiceManager: No active call found with UUID \(uuid)")
            return
        }

        call.disconnect()
        print("TwilioVoiceManager: Call disconnect requested")
    }
}

// MARK: - CallDelegate
extension TwilioVoiceManager: CallDelegate {
    func callDidConnect(call: Call) {
        print("TwilioVoiceManager: Call connected")
        activeCall = call
    }

    func callDidDisconnect(call: Call, error: Error?) {
        if let error = error {
            print("TwilioVoiceManager: Call disconnected with error: \(error.localizedDescription)")
        } else {
            print("TwilioVoiceManager: Call disconnected")
        }
        activeCall = nil
    }

    func callDidFailToConnect(call: Call, error: Error) {
        print("TwilioVoiceManager: Call failed to connect: \(error.localizedDescription)")
        activeCall = nil
    }
}
