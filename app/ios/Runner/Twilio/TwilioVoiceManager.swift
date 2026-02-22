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
