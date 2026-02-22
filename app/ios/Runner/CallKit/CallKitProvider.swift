import Foundation
import CallKit
import AVFoundation

class CallKitProvider: NSObject {
    static let shared = CallKitProvider()

    private let provider: CXProvider
    private let callController = CXCallController()

    private override init() {
        // Configure provider
        let configuration = CXProviderConfiguration(localizedName: "Ella AI")
        configuration.supportsVideo = false
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]

        provider = CXProvider(configuration: configuration)

        super.init()

        provider.setDelegate(self, queue: nil)
    }

    func setup() {
        // Placeholder for initialization
        print("CallKitProvider: setup called")
    }

    func reportIncomingCall(uuid: UUID, caller: String, completion: @escaping (Error?) -> Void) {
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.hasVideo = false
        update.localizedCallerName = caller

        print("CallKitProvider: reporting incoming call from \(caller)")

        provider.reportNewIncomingCall(with: uuid, update: update) { error in
            if let error = error {
                print("CallKitProvider: ERROR reporting call: \(error.localizedDescription)")
            } else {
                print("CallKitProvider: Successfully reported incoming call")
            }
            completion(error)
        }
    }
}

// MARK: - CXProviderDelegate
extension CallKitProvider: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        print("CallKitProvider: provider did reset")
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        print("CallKitProvider: User answered call \(action.callUUID)")

        // TODO: Notify TwilioVoiceManager to accept the call
        // For now, just fulfill the action
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        print("CallKitProvider: User ended call \(action.callUUID)")

        // TODO: Notify TwilioVoiceManager to end the call
        // For now, just fulfill the action
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        print("CallKitProvider: User set mute to \(action.isMuted) for call \(action.callUUID)")

        // TODO: Notify TwilioVoiceManager to mute/unmute
        // For now, just fulfill the action
        action.fulfill()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        print("CallKitProvider: Audio session activated")

        // Configure audio session for VoIP
        do {
            try audioSession.setCategory(.playAndRecord, mode: .voiceChat, options: [])
            try audioSession.setActive(true)
            print("CallKitProvider: Audio session configured for VoIP")
        } catch {
            print("CallKitProvider: ERROR configuring audio session: \(error.localizedDescription)")
        }

        // TODO: Notify TwilioVoiceManager that audio is ready
    }
}
